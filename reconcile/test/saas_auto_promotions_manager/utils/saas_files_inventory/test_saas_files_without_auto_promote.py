from collections.abc import (
    Callable,
    Iterable,
    Mapping,
)

from reconcile.gql_definitions.saas_auto_promotions_manager.saas_files_for_auto_promotion import (
    SaasFileV2,
)
from reconcile.saas_auto_promotions_manager.utils.saas_files_inventory import (
    SaasFilesInventory,
)


def test_saas_files_without_auto_promote(
    saas_files_builder: Callable[[Iterable[Mapping]], list[SaasFileV2]]
):
    saas_files = saas_files_builder(
        [
            {
                "path": "/saas1.yml",
                "name": "saas_1",
                "resourceTemplates": [
                    {
                        "name": "template_1",
                        "url": "repo1/url",
                        "targets": [
                            {
                                "ref": "main",
                                "namespace": {"path": "/namespace1.yml"},
                                "promotion": {
                                    "publish": ["channel-a"],
                                },
                            }
                        ],
                    }
                ],
            },
            {
                "path": "/saas2.yml",
                "name": "saas_2",
                "resourceTemplates": [
                    {
                        "name": "template_2",
                        "url": "repo2/url",
                        "targets": [
                            {
                                "ref": "main",
                                "namespace": {"path": "/namespace2.yml"},
                                "promotion": {
                                    "publish": ["channel-b"],
                                    "subscribe": ["channel-a"],
                                },
                            },
                            {
                                "ref": "main",
                                "namespace": {"path": "/namespace3.yml"},
                            },
                        ],
                    }
                ],
            },
        ]
    )
    inventory = SaasFilesInventory(saas_files=saas_files)
    assert len(inventory.publishers) == 0
    assert len(inventory.subscribers) == 0