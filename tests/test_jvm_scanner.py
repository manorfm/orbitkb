"""`jvm_scanner` replaces tree-sitter-kotlin/-java: that native parser had a proven,
reproducible memory-corruption bug (SIGSEGV/SIGBUS/hangs on a real service) that
subprocess isolation could only contain, not eliminate. Like every other stack's
call resolution (scan_helpers.resolve_local_calls), this trades exact AST precision
for a heuristic that can never crash: brace-counting instead of a real parser.
"""
from __future__ import annotations

from orbitkb.analysis.jvm_scanner import (
    find_calls,
    find_classes,
    find_functions,
    find_matching_brace,
    split_top_level,
)


def test_find_matching_brace_returns_the_paired_closing_brace():
    text = "{ a(); { b(); } c(); }"
    assert find_matching_brace(text, 0) == len(text) - 1


def test_find_matching_brace_ignores_braces_inside_a_string_literal():
    text = '{ val s = "not a } brace"; }'
    assert find_matching_brace(text, 0) == len(text) - 1


def test_find_matching_brace_ignores_braces_inside_a_triple_quoted_string():
    text = '{ val s = """has a } inside"""; }'
    assert find_matching_brace(text, 0) == len(text) - 1


def test_find_matching_brace_ignores_braces_inside_a_char_literal():
    text = "{ val c = '}'; }"
    assert find_matching_brace(text, 0) == len(text) - 1


def test_find_matching_brace_ignores_braces_inside_line_and_block_comments():
    text = "{ // a } b\n /* c } d */ e(); }"
    assert find_matching_brace(text, 0) == len(text) - 1


def test_find_matching_brace_handles_escaped_quotes_inside_a_string():
    text = r'{ val s = "a \" } b"; }'
    assert find_matching_brace(text, 0) == len(text) - 1


def test_find_classes_locates_a_kotlin_class_with_primary_constructor_and_supertype():
    source = (
        "package com.example\n\n"
        "@RestController\n"
        '@RequestMapping("/orders")\n'
        "class OrdersController(private val service: OrdersService) : BaseController() {\n"
        "    fun get() = service.get()\n"
        "}\n"
    )
    classes = find_classes(source)

    assert len(classes) == 1
    cls = classes[0]
    assert cls.name == "OrdersController"
    assert "@RestController" in cls.annotations
    assert "@RequestMapping" in cls.annotations
    assert "private val service: OrdersService" in cls.header
    assert "BaseController" in cls.header
    assert source[cls.body_start] == "{"
    assert source[cls.body_end] == "}"


def test_find_classes_locates_a_java_class_with_extends_and_implements():
    source = (
        "package com.example;\n\n"
        "@RestController\n"
        "public class OrdersController extends BaseController implements Closeable {\n"
        "    private final OrdersService service;\n"
        "}\n"
    )
    classes = find_classes(source)

    assert len(classes) == 1
    cls = classes[0]
    assert cls.name == "OrdersController"
    assert "@RestController" in cls.annotations
    assert "extends BaseController" in cls.header
    assert "implements Closeable" in cls.header


def test_find_classes_does_not_get_confused_by_a_brace_inside_a_string_in_the_body():
    source = (
        "class Foo {\n"
        '    fun template() = "{not a real brace}"\n'
        "    fun real() {}\n"
        "}\n"
        "class Bar {\n"
        "}\n"
    )
    classes = find_classes(source)

    assert [c.name for c in classes] == ["Foo", "Bar"]


def test_find_classes_gives_a_brace_less_class_an_empty_body_even_when_another_class_follows():
    """A primary-constructor-only Kotlin class (no `{}` at all) followed by more code
    used to have its header scan run straight through into the next class's own `{`,
    mistaking it for its own body -- corrupting both classes' extraction.
    """
    source = (
        "open class BaseClient(\n"
        "  protected val stub: Stub,\n"
        ")\n"
        "\n"
        "class CheckoutService(stub: Stub) : BaseClient(stub) {\n"
        "  fun checkout() {}\n"
        "}\n"
    )
    classes = find_classes(source)

    assert [c.name for c in classes] == ["BaseClient", "CheckoutService"]
    base = classes[0]
    assert base.body_end < base.body_start  # empty body: no members to find inside it
    assert "protected val stub: Stub" in base.header
    checkout = classes[1]
    assert "BaseClient(stub)" in checkout.header
    assert source[checkout.body_start] == "{"
    assert source[checkout.body_end] == "}"


def test_find_functions_locates_kotlin_block_and_expression_body_functions():
    class_body = (
        "{\n"
        "    fun blockBody(x: Int): Int {\n"
        "        return x + 1\n"
        "    }\n\n"
        "    fun exprBody(x: Int) = x + 1\n"
        "}\n"
    )
    functions = find_functions(class_body, 0, len(class_body), kotlin=True)

    assert [f.name for f in functions] == ["blockBody", "exprBody"]
    assert "return x + 1" in functions[0].text
    assert functions[1].text.strip().startswith("fun exprBody")


def test_find_functions_handles_a_return_typed_expression_body_through_a_let_chain():
    """A pattern taken from a real Spring controller that used to crash tree-sitter-
    kotlin on this exact repo: a return-type-annotated expression body (`: MenuOut =`)
    whose value is a multi-line `.let { ... }` chain -- the `{` there opens a lambda
    literal, not a function body, and must be depth-tracked like any other bracket
    rather than mistaken for (or confused with) the function's own body.
    """
    class_body = (
        "{\n"
        "    fun get(id: String): MenuOut =\n"
        "        logger.info(\"get menu $id\")\n"
        "            .let { menuService.get(id).out() }\n\n"
        "    fun next() {}\n"
        "}\n"
    )
    functions = find_functions(class_body, 0, len(class_body), kotlin=True)

    assert [f.name for f in functions] == ["get", "next"]
    get = functions[0]
    assert "logger.info" in get.text
    assert get.text.rstrip().endswith("}")  # captures through the .let{} lambda's own close
    calls = [callee for callee, _offset in find_calls(get.text[get.body_offset :])]
    assert "menuService.get" in calls
    assert "out" in calls


def test_find_calls_extracts_a_call_inside_a_safe_call_let_lambda():
    """`?.let { ... }` (a real pattern from this repo's Feign error decoder) must not
    hide the call inside its lambda -- find_calls has no concept of lambda scope, it
    just scans for `identifier(` text, so this should already work, but the safe-call
    `?.` operator right before `.let` is worth locking in explicitly.
    """
    text = 'fun decode(body: String?): JsonNode? = body?.let { objectMapper.readTree(it) }'
    calls = [callee for callee, _offset in find_calls(text)]

    assert "objectMapper.readTree" in calls


def test_find_functions_locates_java_methods_and_skips_the_constructor():
    class_body = (
        "{\n"
        "    public Foo() {}\n\n"
        "    @GetMapping(\"/x\")\n"
        "    public String get(String id) {\n"
        "        return id;\n"
        "    }\n"
        "}\n"
    )
    functions = find_functions(class_body, 0, len(class_body), kotlin=False)

    assert [f.name for f in functions] == ["get"]
    assert "@GetMapping" in functions[0].modifiers
    assert "return id;" in functions[0].text


def test_find_functions_keeps_java_body_after_throws_clause():
    source = "class Config { SecurityFilterChain filter(HttpSecurity http) throws Exception { return http.build(); } }"

    functions = find_functions(source, 0, len(source), kotlin=False)

    assert len(functions) == 1
    assert functions[0].name == "filter"
    assert "return http.build();" in functions[0].text[functions[0].body_offset:]


def test_split_top_level_respects_nested_generics_and_parens():
    assert split_top_level("private val x: Map<String, Int>, val y: Foo(a, b)") == [
        "private val x: Map<String, Int>", " val y: Foo(a, b)",
    ]


def test_split_top_level_handles_a_single_parameter():
    assert split_top_level("private val service: OrdersService") == ["private val service: OrdersService"]


def test_split_top_level_handles_an_empty_string():
    assert split_top_level("") == []
    assert split_top_level("   ") == []


def test_find_calls_extracts_dotted_callee_and_skips_control_flow_keywords():
    text = "if (x) { repository.save(item); helper(); for (i in xs) { foo.bar(); } }"
    calls = [callee for callee, _offset in find_calls(text)]

    assert "repository.save" in calls
    assert "helper" in calls
    assert "foo.bar" in calls
    assert "if" not in calls
    assert "for" not in calls


def test_find_calls_skips_object_construction():
    text = "val x = new Foo(1, 2); val y = Bar()"
    calls = [callee for callee, _offset in find_calls(text)]

    assert "Foo" not in calls
    assert "Bar" in calls
