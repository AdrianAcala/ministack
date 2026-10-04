# Copyright (c) 2026 MiniStack Contributors. SPDX-License-Identifier: MIT
"""SES tenant lifecycle, resource associations, and AWS-observed errors."""

import copy
import json
import re
import uuid

import pytest
from botocore.exceptions import ClientError

from ministack.core.responses import set_request_account_id, set_request_region


def _error(client, operation, code, message, **params):
    with pytest.raises(ClientError) as caught:
        getattr(client, operation)(**params)
    response = caught.value.response
    assert response["Error"] == {"Code": code, "Message": message}
    assert response["ResponseMetadata"]["HTTPStatusCode"] == (404 if code == "NotFoundException" else 400)


def test_tenant_configuration_set_association_lifecycle(sesv2):
    name = "tenant-" + uuid.uuid4().hex[:10]
    tags = [{"Key": "purpose", "Value": "regression"}]
    sesv2.create_configuration_set(ConfigurationSetName=name, Tags=tags)
    tenant = sesv2.create_tenant(TenantName=name, Tags=tags)
    assert re.fullmatch(r"tn-[a-f0-9]{28}", tenant["TenantId"])
    assert tenant["TenantArn"].endswith(f"tenant/{name}/{tenant['TenantId']}")
    assert tenant["SendingStatus"] == "ENABLED"
    assert tenant["Tags"] == tags
    assert sesv2.get_tenant(TenantName=name)["Tenant"] == {k: v for k, v in tenant.items() if k != "ResponseMetadata"}
    _error(
        sesv2,
        "create_tenant",
        "AlreadyExistsException",
        f"Tenant with name {name} already exists in account 000000000000",
        TenantName=name,
    )
    listed = sesv2.list_tenants(Filter={"TENANT_NAME_CONTAINS": name, "SENDING_STATUS": "ENABLED"})["Tenants"]
    assert listed == [{k: v for k, v in tenant.items() if k not in ("ResponseMetadata", "Tags")}]
    assert sesv2.list_tags_for_resource(ResourceArn=tenant["TenantArn"])["Tags"] == tags
    sesv2.tag_resource(ResourceArn=tenant["TenantArn"], Tags=[{"Key": "updated", "Value": "yes"}])
    sesv2.untag_resource(ResourceArn=tenant["TenantArn"], TagKeys=["purpose"])
    assert sesv2.get_tenant(TenantName=name)["Tenant"]["Tags"] == [{"Key": "updated", "Value": "yes"}]
    arn = f"arn:aws:ses:us-east-1:000000000000:configuration-set/{name}"
    sesv2.create_tenant_resource_association(TenantName=name, ResourceArn=arn)
    _error(
        sesv2,
        "create_tenant_resource_association",
        "AlreadyExistsException",
        f"Resources {arn} has already been associated with tenant {name}",
        TenantName=name,
        ResourceArn=arn,
    )
    assert sesv2.list_tenant_resources(TenantName=name)["TenantResources"] == [
        {"ResourceType": "configuration-set", "ResourceArn": arn}
    ]
    inverse = sesv2.list_resource_tenants(ResourceArn=arn)["ResourceTenants"]
    assert inverse[0]["TenantId"] == tenant["TenantId"]
    assert inverse[0]["ResourceArn"] == arn
    _error(
        sesv2,
        "delete_configuration_set",
        "BadRequestException",
        f"Cannot delete <{arn}> because it has tenant associations. Remove all tenant associations and try again.",
        ConfigurationSetName=name,
    )
    sesv2.delete_tenant_resource_association(TenantName=name, ResourceArn=arn)
    sesv2.delete_tenant_resource_association(TenantName=name, ResourceArn=arn)
    assert sesv2.list_tenant_resources(TenantName=name)["TenantResources"] == []
    sesv2.delete_tenant(TenantName=name)
    _error(sesv2, "get_tenant", "NotFoundException", f"The requested tenant <{name}> does not exist.", TenantName=name)
    _error(
        sesv2,
        "list_tags_for_resource",
        "NotFoundException",
        f"No Tenant present with name: {name}with tenantId: {tenant['TenantId']}",
        ResourceArn=tenant["TenantArn"],
    )
    for operation, params in [
        ("tag_resource", {"Tags": [{"Key": "purpose", "Value": "regression"}]}),
        ("untag_resource", {"TagKeys": ["purpose"]}),
    ]:
        _error(sesv2, operation, "NotFoundException", f"No Tenant present with name: {name}with tenantId: {tenant['TenantId']}",
               ResourceArn=tenant["TenantArn"], **params)
    sesv2.delete_configuration_set(ConfigurationSetName=name)


def test_tenant_invalid_and_missing_resources(sesv2):
    name = "tenant-errors-" + uuid.uuid4().hex[:10]
    _error(
        sesv2,
        "create_tenant",
        "BadRequestException",
        f"Invalid tenant name <{name} bad>: only alphanumeric ASCII characters, '_', and '-' are allowed.",
        TenantName=name + " bad",
    )
    _error(
        sesv2,
        "create_tenant_resource_association",
        "NotFoundException",
        f"The requested tenant <{name}> does not exist.",
        TenantName=name,
        ResourceArn="invalid-arn",
    )
    sesv2.create_tenant(TenantName=name)
    _error(
        sesv2,
        "create_tenant_resource_association",
        "BadRequestException",
        "Provided resource identifier is not an SES resource",
        TenantName=name,
        ResourceArn="invalid-arn",
    )
    arn = f"arn:aws:ses:us-east-1:000000000000:configuration-set/{name}"
    _error(
        sesv2,
        "create_tenant_resource_association",
        "NotFoundException",
        f"Configuration set <{name}> does not exist:",
        TenantName=name,
        ResourceArn=arn,
    )
    other = arn.replace("us-east-1", "us-west-2")
    _error(
        sesv2,
        "create_tenant_resource_association",
        "BadRequestException",
        f"Resource <{other}> must be in the same region",
        TenantName=name,
        ResourceArn=other,
    )
    sesv2.delete_tenant(TenantName=name)


@pytest.mark.parametrize("filters,message", [
    ({"INVALID": "template"},
     "1 validation error detected: Value at 'filter' failed to satisfy constraint: Map keys must satisfy constraint: [Member must satisfy enum value set: [RESOURCE_TYPE]]"),
    ({"RESOURCE_TYPE": "bogus"}, "Invalid resource type bogus specified."),
    ({"RESOURCE_TYPE": "template,configuration-set"},
     "Invalid resource type template,configuration-set specified."),
])
def test_tenant_resource_filter_errors(sesv2, filters, message):
    name = "tenant-filter-" + uuid.uuid4().hex[:10]
    sesv2.create_tenant(TenantName=name)
    _error(sesv2, "list_tenant_resources", "BadRequestException", message,
           TenantName=name, Filter=filters)
    sesv2.delete_tenant(TenantName=name)


def test_tenant_sending_status_filter(sesv2):
    name = "tenant-status-" + uuid.uuid4().hex[:10]
    sesv2.create_tenant(TenantName=name)
    _error(sesv2, "list_tenants", "BadRequestException", "Invalid sending status <bogus>.",
           Filter={"TENANT_NAME_CONTAINS": name, "SENDING_STATUS": "bogus"})
    for status in ("REINSTATED", "DISABLED"):
        assert sesv2.list_tenants(Filter={"TENANT_NAME_CONTAINS": name, "SENDING_STATUS": status})["Tenants"] == []
    sesv2.delete_tenant(TenantName=name)


def test_tenant_template_association_and_delete_with_associations(sesv2):
    name = "tenant-template-" + uuid.uuid4().hex[:10]
    sesv2.create_email_template(TemplateName=name, TemplateContent={"Subject": "probe", "Text": "probe"})
    sesv2.create_configuration_set(ConfigurationSetName=name)
    template_arn = f"arn:aws:ses:us-east-1:000000000000:template/{name}"
    config_arn = f"arn:aws:ses:us-east-1:000000000000:configuration-set/{name}"
    tenants = [name + suffix for suffix in ("-a", "-b")]
    for tenant_name in tenants:
        sesv2.create_tenant(TenantName=tenant_name)
        for arn in (config_arn, template_arn):
            sesv2.create_tenant_resource_association(TenantName=tenant_name, ResourceArn=arn)
        assert sesv2.list_tenant_resources(TenantName=tenant_name, Filter={"RESOURCE_TYPE": "template"})["TenantResources"] == [
            {"ResourceType": "template", "ResourceArn": template_arn}
        ]
        first = sesv2.list_tenant_resources(TenantName=tenant_name, PageSize=1)
        second = sesv2.list_tenant_resources(TenantName=tenant_name, PageSize=1, NextToken=first["NextToken"])
        assert first["TenantResources"] + second["TenantResources"] == [
            {"ResourceType": "configuration-set", "ResourceArn": config_arn},
            {"ResourceType": "template", "ResourceArn": template_arn},
        ]
        assert "NextToken" not in second
    first = sesv2.list_resource_tenants(ResourceArn=config_arn, PageSize=1)
    second = sesv2.list_resource_tenants(ResourceArn=config_arn, PageSize=1, NextToken=first["NextToken"])
    assert [item["TenantName"] for item in first["ResourceTenants"] + second["ResourceTenants"]] == tenants
    assert "NextToken" not in second
    sesv2.delete_tenant(TenantName=tenants[0])
    assert [item["TenantName"] for item in sesv2.list_resource_tenants(ResourceArn=config_arn)["ResourceTenants"]] == tenants[1:]
    _error(sesv2, "delete_configuration_set", "BadRequestException",
           f"Cannot delete <{config_arn}> because it has tenant associations. Remove all tenant associations and try again.",
           ConfigurationSetName=name)
    sesv2.delete_tenant(TenantName=tenants[1])
    assert sesv2.list_resource_tenants(ResourceArn=config_arn)["ResourceTenants"] == []
    sesv2.delete_configuration_set(ConfigurationSetName=name)
    sesv2.delete_email_template(TemplateName=name)


def test_tenant_state_roundtrip_and_account_region_isolation():
    from ministack.core.persistence import _json_default, _json_object_hook
    from ministack.services import ses_v2

    snapshot = copy.deepcopy(ses_v2.get_state())
    scopes = [
        ("000000000000", "us-east-1"),
        ("111111111111", "us-east-1"),
        ("000000000000", "us-west-2"),
    ]
    saved_tenants = {}
    try:
        for account, region in scopes:
            set_request_account_id(account)
            set_request_region(region)
            assert "same" not in ses_v2._tenants
            assert "same" not in ses_v2._tenant_resources
            status, _, body = ses_v2._tenant_request(
                "POST", "/tenants", {"TenantName": "same", "Tags": [{"Key": "region", "Value": region}]}
            )
            assert status == 200
            saved_tenants[(account, region)] = json.loads(body)
            arn = f"arn:aws:ses:{region}:{account}:configuration-set/same"
            ses_v2._config_sets["same"] = {"ConfigurationSetName": "same"}
            assert ses_v2._tenant_request(
                "POST", "/tenants/resources", {"TenantName": "same", "ResourceArn": arn}
            )[0] == 200

        encoded = json.dumps(ses_v2.get_state(), default=_json_default, sort_keys=True)
        saved = json.loads(encoded, object_hook=_json_object_hook)
        ses_v2.reset()
        for account, region in scopes:
            set_request_account_id(account)
            set_request_region(region)
            assert not ses_v2._tenants
            assert not ses_v2._tenant_resources
        ses_v2.load_persisted_state(saved)
        assert json.dumps(ses_v2.get_state(), default=_json_default, sort_keys=True) == encoded
        for account, region in scopes:
            set_request_account_id(account)
            set_request_region(region)
            tenant = saved_tenants[(account, region)]
            assert ses_v2._tenants["same"] == tenant
            assert ses_v2._ses_tags[tenant["TenantArn"]] == [{"Key": "region", "Value": region}]
            arn = f"arn:aws:ses:{region}:{account}:configuration-set/same"
            assert list(ses_v2._tenant_resources["same"]) == [arn]
            inverse = json.loads(ses_v2._tenant_request(
                "POST", "/resources/tenants/list", {"ResourceArn": arn}
            )[2])["ResourceTenants"]
            assert inverse == [{
                "TenantName": "same",
                "TenantId": tenant["TenantId"],
                "ResourceArn": arn,
                "AssociatedTimestamp": ses_v2._tenant_resources["same"][arn],
            }]
    finally:
        ses_v2.reset()
        ses_v2.load_persisted_state(snapshot)
