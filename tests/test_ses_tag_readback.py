# Copyright (c) 2026 MiniStack Contributors. SPDX-License-Identifier: MIT
"""SES configuration-set readback errors observed on real AWS."""

import uuid

import pytest
from botocore.exceptions import ClientError


def _error(client, operation, code, message, **params):
    with pytest.raises(ClientError) as caught:
        getattr(client, operation)(**params)
    response = caught.value.response
    assert response["Error"] == {"Code": code, "Message": message}
    assert response["ResponseMetadata"]["HTTPStatusCode"] == (404 if code == "NotFoundException" else 400)


def test_configuration_set_tag_readback_errors(sesv2):
    name = "tags-" + uuid.uuid4().hex[:10]
    sesv2.create_configuration_set(ConfigurationSetName=name, Tags=[{"Key": "keep", "Value": "yes"}])
    arn = f"arn:aws:ses:us-east-1:000000000000:configuration-set/{name}"
    _error(
        sesv2,
        "list_tags_for_resource",
        "BadRequestException",
        "ResourceArn is not the expected format",
        ResourceArn="invalid-arn",
    )
    _error(
        sesv2,
        "list_tags_for_resource",
        "NotFoundException",
        f"No ConfigurationSet present with name: {name}-missing",
        ResourceArn=arn + "-missing",
    )
    assert sesv2.list_tags_for_resource(ResourceArn=arn.replace(":ses:", ":s3:"))["Tags"] == [
        {"Key": "keep", "Value": "yes"}
    ]
    assert sesv2.list_tags_for_resource(ResourceArn=arn.replace("us-east-1", "us-west-2"))["Tags"] == []
    _error(
        sesv2,
        "list_tags_for_resource",
        "BadRequestException",
        "Operations on a resource created in a different account is not allowed",
        ResourceArn=arn.replace("000000000000", "111111111111"),
    )
    _error(sesv2, "list_tags_for_resource", "ValidationException", "", ResourceArn=arn + " bad")
    sesv2.delete_configuration_set(ConfigurationSetName=name)


def test_get_missing_configuration_set(sesv2):
    name = "missing-" + uuid.uuid4().hex[:10]
    _error(
        sesv2,
        "get_configuration_set",
        "NotFoundException",
        f"Configuration set <{name}> does not exist.",
        ConfigurationSetName=name,
    )


@pytest.mark.parametrize("kind,label", [("identity", "EmailIdentity"), ("template", "Template")])
def test_missing_tag_resource(sesv2, kind, label):
    name = "missing-" + uuid.uuid4().hex[:10]
    arn = f"arn:aws:ses:us-east-1:000000000000:{kind}/{name}"
    _error(
        sesv2, "list_tags_for_resource", "NotFoundException", f"No {label} present with name: {name}", ResourceArn=arn
    )


@pytest.mark.parametrize("partition", ["aws-cn", "aws-us-gov", "bogus"])
def test_configuration_set_tag_read_ignores_arn_partition(sesv2, partition):
    name = "partition-" + uuid.uuid4().hex[:10]
    tags = [{"Key": "keep", "Value": "yes"}]
    sesv2.create_configuration_set(ConfigurationSetName=name, Tags=tags)
    try:
        arn = f"arn:{partition}:ses:us-east-1:000000000000:configuration-set/{name}"
        assert sesv2.list_tags_for_resource(ResourceArn=arn)["Tags"] == tags
    finally:
        sesv2.delete_configuration_set(ConfigurationSetName=name)


@pytest.mark.parametrize(
    "arn",
    [
        "arn::ses:us-east-1:000000000000:configuration-set/missing",
        "arn:aws::us-east-1:000000000000:configuration-set/missing",
        "arn:aws:ses:us-east-1::configuration-set/missing",
        "arn:aws:ses:us-east-1:000000000000:configuration-set/name:extra",
        "arn:aws:ses:us-east-1:000000000000:configuration-set/ ",
        "arn:aws:ses:us-east-1:000000000000:template/name:extra",
    ],
)
def test_tag_read_rejects_malformed_arn_before_resource_lookup(sesv2, arn):
    _error(
        sesv2,
        "list_tags_for_resource",
        "BadRequestException",
        "ResourceArn is not the expected format",
        ResourceArn=arn,
    )


@pytest.mark.parametrize(
    "resource",
    [
        "identity/missing.example.com/extra",
        "identity/missing.example.com/",
        "identity/missing.example.com:extra",
        "identity/missing.example.com bad",
    ],
)
def test_identity_tag_read_rejects_invalid_identity_identifier(sesv2, resource):
    _error(
        sesv2,
        "list_tags_for_resource",
        "BadRequestException",
        "Provided resource identifier is not an SES resource",
        ResourceArn=f"arn:aws:ses:us-east-1:000000000000:{resource}",
    )


@pytest.mark.parametrize("account", ["", "12345678901", "1234567890123", "not-an-account"])
def test_identity_tag_read_validates_account_shape_before_account_match(sesv2, account):
    _error(
        sesv2,
        "list_tags_for_resource",
        "BadRequestException",
        "Provided resource identifier is not an SES resource",
        ResourceArn=f"arn:aws:ses:us-east-1:{account}:identity/missing.example.com",
    )


@pytest.mark.parametrize("name", ["bad name", "badé"])
def test_template_tag_read_validates_template_name(sesv2, name):
    _error(
        sesv2,
        "list_tags_for_resource",
        "BadRequestException",
        "Template name can contain alphanumeric characters, underscores(_) and hyphens(-). "
        "It can contain up to 128 characters and it should start with an alphanumeric character.",
        ResourceArn=f"arn:aws:ses:us-east-1:000000000000:template/{name}",
    )


def test_get_configuration_set_rejects_invalid_name_before_lookup(sesv2):
    _error(
        sesv2,
        "get_configuration_set",
        "BadRequestException",
        "Invalid configuration set name <bad name>: only alphanumeric ASCII characters, '_', and '-' are allowed.",
        ConfigurationSetName="bad name",
    )


@pytest.mark.parametrize("name", ["_name", "name-", "foo.123", "a..b", "a" * 64])
def test_identity_tag_read_rejects_invalid_domain_names(sesv2, name):
    _error(
        sesv2,
        "list_tags_for_resource",
        "BadRequestException",
        "Provided resource identifier is not an SES resource",
        ResourceArn=f"arn:aws:ses:us-east-1:000000000000:identity/{name}",
    )


@pytest.mark.parametrize(
    "name",
    [
        "a_b.example.com",
        "user/name@example.com",
        "user+tag@example.com",
        '"user:name"@example.com',
        '"user@name"@example.com',
        '"user name"@example.com',
        "user@-example.com",
        "user@" + "a" * 64 + ".com",
    ],
)
def test_identity_tag_read_accepts_distinct_domain_and_email_shapes(sesv2, name):
    _error(
        sesv2,
        "list_tags_for_resource",
        "NotFoundException",
        f"No EmailIdentity present with name: {name}",
        ResourceArn=f"arn:aws:ses:us-east-1:000000000000:identity/{name}",
    )


@pytest.mark.parametrize(
    "kind,name",
    [("template", "_name"), ("template", "-name"), ("template", "n" * 128), ("configuration-set", "n" * 64)],
)
def test_tag_read_accepts_valid_name_boundaries(sesv2, kind, name):
    label = "Template" if kind == "template" else "ConfigurationSet"
    _error(
        sesv2,
        "list_tags_for_resource",
        "NotFoundException",
        f"No {label} present with name: {name}",
        ResourceArn=f"arn:aws:ses:us-east-1:000000000000:{kind}/{name}",
    )


def test_get_configuration_set_validates_name_length(sesv2):
    _error(
        sesv2,
        "get_configuration_set",
        "BadRequestException",
        "Configuration set name cannot exceed 64 characters.",
        ConfigurationSetName="n" * 65,
    )
    name = "n" * 64
    _error(
        sesv2,
        "get_configuration_set",
        "NotFoundException",
        f"Configuration set <{name}> does not exist.",
        ConfigurationSetName=name,
    )


@pytest.mark.parametrize(
    "kind,name,message",
    [
        ("identity", "bad name", "Provided resource identifier is not an SES resource"),
        ("configuration-set", "bad name", "Operations on a resource created in a different account is not allowed"),
        ("template", "bad name", "Operations on a resource created in a different account is not allowed"),
        ("configuration-set", "bad:name", "ResourceArn is not the expected format"),
    ],
)
def test_tag_read_preserves_combined_validation_error_precedence(sesv2, kind, name, message):
    _error(
        sesv2,
        "list_tags_for_resource",
        "BadRequestException",
        message,
        ResourceArn=f"arn:aws:ses:us-east-1:111111111111:{kind}/{name}",
    )
