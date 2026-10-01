from orbitkb.discovery.node_imports import parse_node_named_imports


def test_parses_a_simple_named_import():
    source = 'import { SQSClient } from "@aws-sdk/client-sqs";\n'

    assert parse_node_named_imports(source) == [("SQSClient", "client-sqs", "SQSClient")]


def test_parses_multiple_names_in_one_import():
    source = 'import { SQSClient, SendMessageCommand } from "@aws-sdk/client-sqs";\n'

    assert parse_node_named_imports(source) == [
        ("SQSClient", "client-sqs", "SQSClient"),
        ("SendMessageCommand", "client-sqs", "SendMessageCommand"),
    ]


def test_resolves_an_import_alias_to_its_original_name():
    source = 'import { PutObjectCommand as PutCmd } from "@aws-sdk/client-s3";\n'

    assert parse_node_named_imports(source) == [("PutCmd", "client-s3", "PutObjectCommand")]


def test_module_basename_strips_the_scope_segment():
    source = 'import { BlobServiceClient } from "@azure/storage-blob";\n'

    assert parse_node_named_imports(source) == [("BlobServiceClient", "storage-blob", "BlobServiceClient")]


def test_unrelated_import_is_still_parsed_generically():
    source = 'import { Something } from "./local-module";\n'

    assert parse_node_named_imports(source) == [("Something", "local-module", "Something")]


def test_no_imports_returns_empty_list():
    assert parse_node_named_imports("const x = 1;\n") == []
