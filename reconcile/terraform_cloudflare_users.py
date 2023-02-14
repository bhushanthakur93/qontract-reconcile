import sys
from dataclasses import dataclass
from typing import (
    Any,
    Iterable,
    Mapping,
    MutableMapping,
    Optional,
    Set,
    Tuple,
)

from reconcile import queries
from reconcile.gql_definitions.terraform_cloudflare_users import (
    terraform_cloudflare_roles,
)
from reconcile.gql_definitions.terraform_cloudflare_users.terraform_cloudflare_roles import (
    CloudflareAccountRoleQueryData,
    CloudflareAccountRoleV1,
)
from reconcile.status import ExitCodes
from reconcile.utils import gql
from reconcile.utils.external_resource_spec import ExternalResourceSpec
from reconcile.utils.runtime.integration import QontractReconcileIntegration
from reconcile.utils.secret_reader import SecretReader
from reconcile.utils.semver_helper import make_semver
from reconcile.utils.terraform import safe_resource_id
from reconcile.utils.terraform.config_client import (
    ClientAlreadyRegisteredError,
    TerraformConfigClientCollection,
)
from reconcile.utils.terraform_client import run_terraform
from reconcile.utils.terrascript.cloudflare_client import (
    AccountShardingStrategy,
    TerrascriptCloudflareClientFactory,
    validate_terraform_state_for_cloudflare_client,
)
from reconcile.utils.terrascript.models import (
    CloudflareAccount,
    Integration,
    TerraformStateS3,
)

QONTRACT_INTEGRATION = "terraform_cloudflare_users"
QONTRACT_INTEGRATION_VERSION = make_semver(0, 1, 0)
QONTRACT_TF_PREFIX = "qrtfcfusers"
CLOUDFLARE_EMAIL_DOMAIN_ALLOW_LIST_KEY = "cloudflareEmailDomainAllowList"


@dataclass
class CloudflareUser:
    email_address: str
    account_name: str
    org_username: str
    roles: Set[str]


class TerraformCloudflareUsers(QontractReconcileIntegration):
    @property
    def name(self) -> str:
        return QONTRACT_INTEGRATION.replace("_", "-")

    def get_early_exit_desired_state(
        self, *args: Any, **kwargs: Any
    ) -> Optional[dict[str, Any]]:

        cloudflare_roles, settings = self._get_desired_state()

        return {
            "cloudflare_roles": cloudflare_roles.dict(),
            CLOUDFLARE_EMAIL_DOMAIN_ALLOW_LIST_KEY: settings.get(
                CLOUDFLARE_EMAIL_DOMAIN_ALLOW_LIST_KEY
            ),
        }

    def _get_desired_state(
        self,
    ) -> Tuple[CloudflareAccountRoleQueryData, Mapping[str, Any]]:
        cloudflare_roles = terraform_cloudflare_roles.query(
            query_func=gql.get_api().query
        )

        settings = queries.get_app_interface_settings()
        return cloudflare_roles, settings

    def run(self, dry_run: bool, *args: Any, **kwargs: Any) -> None:
        print_to_file = args[0]
        account_name = args[1]
        thread_pool_size = args[2]
        enable_deletion = args[3]

        cloudflare_roles, settings = self._get_desired_state()

        secret_reader = SecretReader(settings=settings)

        cf_clients = self._build_cloudflare_terraform_config_client_collection(
            cloudflare_roles, secret_reader, account_name
        )

        users = get_cloudflare_users(
            cloudflare_roles.cloudflare_account_roles,
            account_name,
            settings.get(CLOUDFLARE_EMAIL_DOMAIN_ALLOW_LIST_KEY),
        )
        specs = build_external_resource_spec_from_cloudflare_users(users)

        cf_clients.add_specs(specs)
        cf_clients.populate_resources()

        working_dirs = cf_clients.dump(print_to_file=print_to_file)

        if print_to_file:
            sys.exit(ExitCodes.SUCCESS)

        # for storing unique CloudflareAccountV1 since cloudflare_account_role_v1 can contain duplicates due to schema
        account_names_to_account = {
            role.account.name: role.account
            for role in cloudflare_roles.cloudflare_account_roles or []
            if role.account.name in cf_clients.dump()
        }

        accounts = [
            acct.dict(by_alias=True) for _, acct in account_names_to_account.items()
        ]

        run_terraform(
            QONTRACT_INTEGRATION,
            QONTRACT_INTEGRATION_VERSION,
            QONTRACT_TF_PREFIX,
            dry_run,
            enable_deletion,
            thread_pool_size,
            working_dirs,
            accounts,
        )

    def _build_cloudflare_terraform_config_client_collection(
        self,
        query_data: CloudflareAccountRoleQueryData,
        secret_reader: SecretReader,
        account_name: str,
    ) -> TerraformConfigClientCollection:
        cf_clients = TerraformConfigClientCollection()
        for role in query_data.cloudflare_account_roles or []:
            if account_name and role.account.name != account_name:
                continue
            cf_account = CloudflareAccount(
                role.account.name,
                role.account.api_credentials.path,
                role.account.enforce_twofactor,
                role.account.q_type,
                role.account.provider_version,
            )

            tf_state = role.account.terraform_state_account.terraform_state

            bucket = tf_state.bucket if tf_state is not None else None
            region = tf_state.region if tf_state is not None else None
            if not bucket:
                raise ValueError("Terraform state must have bucket defined")
            if not region:
                raise ValueError("Terraform state must have region defined")

            temp_integrations = tf_state.integrations if tf_state is not None else []
            integrations: Iterable[Integration] = [
                Integration(i.integration, i.key) for i in temp_integrations
            ]

            tf_state_s3 = TerraformStateS3(
                role.account.terraform_state_account.automation_token.path,
                bucket,
                region,
                integrations,
            )

            validate_terraform_state_for_cloudflare_client(
                QONTRACT_INTEGRATION, tf_state_s3
            )
            client = TerrascriptCloudflareClientFactory.get_client(
                QONTRACT_INTEGRATION,
                tf_state_s3,
                cf_account,
                AccountShardingStrategy(cf_account),
                secret_reader,
                True,
            )

            try:
                cf_clients.register_client(cf_account.name, client)
            except ClientAlreadyRegisteredError:
                pass

        return cf_clients


def get_cloudflare_users(
    cloudflare_roles: Optional[Iterable[CloudflareAccountRoleV1]],
    account_name: Optional[str],
    email_domain_allow_list: Optional[Iterable[str]],
) -> Mapping[str, Mapping[str, CloudflareUser]]:
    """
    Builds a two-level dictionary of users with 1st level keys mapping to cloudflare account names
    and 2nd level keys mapping to user's email address.
    The method also takes into consideration account_name and email_domain_allow_list which can be
    used to filter users not matching these parameters
    """
    users: dict[str, dict[str, CloudflareUser]] = {}

    for cf_role in cloudflare_roles or []:
        if account_name and cf_role.account.name != account_name:
            continue
        for access_role in cf_role.access_roles or []:
            for user in access_role.users:
                if user.cloudflare_user is not None and (
                    user.cloudflare_user.split("@")[1]
                    in (email_domain_allow_list or [])
                ):
                    temp = users.get(cf_role.account.name)
                    if temp is not None:
                        if temp.get(user.cloudflare_user) is not None:
                            users[cf_role.account.name][
                                user.cloudflare_user
                            ].roles.update(set(cf_role.roles))
                        else:
                            users[cf_role.account.name][
                                user.cloudflare_user
                            ] = CloudflareUser(
                                user.cloudflare_user,
                                cf_role.account.name,
                                user.org_username,
                                set(cf_role.roles),
                            )

                    else:
                        users[cf_role.account.name] = {
                            user.cloudflare_user: CloudflareUser(
                                user.cloudflare_user,
                                cf_role.account.name,
                                user.org_username,
                                set(cf_role.roles),
                            )
                        }

    return users


def build_external_resource_spec_from_cloudflare_users(
    cloudflare_users: Mapping[str, Mapping[str, CloudflareUser]]
) -> Iterable[ExternalResourceSpec]:
    specs: list[ExternalResourceSpec] = []

    for _, v in cloudflare_users.items():
        for _, cf_user in v.items():
            data_source_cloudflare_account_roles = {
                "identifier": safe_resource_id(cf_user.account_name),
                "account_id": "${var.account_id}",
            }

            cloudflare_account_member = {
                "provider": "cloudflare_account_member",
                "identifier": safe_resource_id(cf_user.org_username),
                "email_address": cf_user.email_address,
                "account_id": "${var.account_id}",
                "role_ids": [
                    # I know this is ugly :(
                    f'%{{ for role in data.cloudflare_account_roles.{safe_resource_id(cf_user.account_name)}.roles ~}}  %{{if role.name == "{each}" ~}}${{role.id}}%{{ endif ~}}  %{{ endfor ~}}'
                    for each in cf_user.roles
                ],
                "cloudflare_account_roles": data_source_cloudflare_account_roles,
            }
            specs.append(
                _get_external_spec_from_resource(
                    cloudflare_account_member, cf_user.account_name
                )
            )

    return specs


def _get_external_spec_from_resource(
    resource: MutableMapping[Any, Any], account_name: str
) -> ExternalResourceSpec:
    return ExternalResourceSpec(
        provision_provider="cloudflare",
        provisioner={"name": f"{account_name}"},
        namespace={},
        resource=resource,
    )