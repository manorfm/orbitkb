from orbitkb.discovery.go_imports import parse_go_import_paths


def test_parses_a_single_import_with_default_alias():
    source = 'import "github.com/aws/aws-sdk-go-v2/service/sqs"\n'

    assert parse_go_import_paths(source) == {"sqs": "github.com/aws/aws-sdk-go-v2/service/sqs"}


def test_parses_an_explicit_alias():
    source = 'import awssqs "github.com/aws/aws-sdk-go-v2/service/sqs"\n'

    assert parse_go_import_paths(source) == {"awssqs": "github.com/aws/aws-sdk-go-v2/service/sqs"}


def test_parses_a_grouped_import_block():
    source = (
        "import (\n"
        '\t"github.com/aws/aws-sdk-go-v2/service/sqs"\n'
        '\t"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob"\n'
        ")\n"
    )

    assert parse_go_import_paths(source) == {
        "sqs": "github.com/aws/aws-sdk-go-v2/service/sqs",
        "azblob": "github.com/Azure/azure-sdk-for-go/sdk/storage/azblob",
    }


def test_blank_and_dot_imports_are_excluded():
    source = 'import _ "github.com/lib/pq"\nimport . "fmt"\n'

    assert parse_go_import_paths(source) == {}


def test_no_imports_returns_empty_dict():
    assert parse_go_import_paths("package main\n") == {}
