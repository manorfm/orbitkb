from orbitkb.analysis.engine import StaticAnalysisEngine


def _analyze(tmp_path, *, import_mapper=True, mapper_body="BillOut(value)", duplicate=False,
             controller_body="repository.load().resumeOut(1)"):
    import_line = "import example.output.resumeOut" if import_mapper else ""
    (tmp_path / "Controller.kt").write_text(f'''
package example.web
{import_line}
import org.springframework.web.bind.annotation.GetMapping

class Controller {{
    @GetMapping("/orders")
    fun get() = {controller_body}
}}
''')
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    (output / "BillOut.kt").write_text(f'''
package example.output

data class BillOut(val id: String)
fun Bill.resumeOut(value: Int) = {mapper_body}
''')
    if duplicate:
        (output / "Duplicate.kt").write_text('''
package example.output
fun Bill.resumeOut(value: Int) = BillOut(value.toString())
''')
    return StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")


def test_unique_imported_extension_with_direct_dto_constructor_proves_response(tmp_path):
    analysis = _analyze(tmp_path, mapper_body="BillOut(value.toString())")

    response = analysis.contracts["Controller.get"]["returns"]
    assert response["type"] == "BillOut"
    assert response["fields"] == [
        {"name": "id", "type": "String", "required": True, "validations": []},
    ]
    assert response["derived_from"]["file"] == "output/BillOut.kt"
    assert response["confidence"] == "inferred"


def test_unimported_or_ambiguous_extension_keeps_response_unknown(tmp_path):
    assert _analyze(tmp_path, import_mapper=False).contracts["Controller.get"]["returns"] is None
    assert _analyze(tmp_path, duplicate=True).contracts["Controller.get"]["returns"] is None


def test_extension_with_unproven_expression_keeps_response_unknown(tmp_path):
    analysis = _analyze(tmp_path, mapper_body="if (value > 0) BillOut(value.toString()) else fallback()")

    assert analysis.contracts["Controller.get"]["returns"] is None


def test_conditional_or_nullable_controller_expression_keeps_response_unknown(tmp_path):
    conditional = _analyze(tmp_path, controller_body="if (flag) fallback() else repository.load().resumeOut(1)")
    nullable = _analyze(tmp_path, controller_body="repository.load()?.resumeOut(1)")

    assert conditional.contracts["Controller.get"]["returns"] is None
    assert nullable.contracts["Controller.get"]["returns"] is None


def test_receiver_type_must_match_the_unique_injected_interface_method(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "BillOut.kt").write_text('''
package example.output
import example.domain.Bill
data class BillOut(val id: String)
fun Bill.resumeOut() = BillOut(id)
''')
    domain = tmp_path / "domain"
    domain.mkdir()
    (domain / "Bill.kt").write_text("package example.domain\nclass Bill(val id: String)\n")
    command = tmp_path / "command"
    command.mkdir()
    interface = command / "Lookup.kt"
    interface.write_text('''
package example.command
import example.domain.Bill
interface Lookup {
    fun load(): Bill
}
''')
    controller = tmp_path / "Controller.kt"
    controller.write_text('''
package example.web
import example.command.Lookup
import example.output.resumeOut
import org.springframework.web.bind.annotation.GetMapping
class Controller(private val lookup: Lookup) {
    @GetMapping("/orders")
    fun get() = lookup.load().resumeOut()
}
''')

    matched = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")
    assert matched.contracts["Controller.get"]["returns"]["confidence"] == "confirmed"
    assert matched.contracts["Controller.get"]["returns"]["receiver_evidence"]["file"] == "command/Lookup.kt"

    interface.write_text(interface.read_text().replace("fun load(): Bill", "fun load(): String"))
    mismatched = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")
    assert mismatched.contracts["Controller.get"]["returns"]["confidence"] == "inferred"

    interface.write_text(interface.read_text().replace("fun load(): String", "fun load(): Bill?"))
    nullable = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")
    assert nullable.contracts["Controller.get"]["returns"]["confidence"] == "inferred"

    interface.write_text(interface.read_text().replace("fun load(): Bill?", "fun load(): Bill\n    fun load(value: Int): Bill"))
    overloaded = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")
    assert overloaded.contracts["Controller.get"]["returns"]["confidence"] == "inferred"
