from orbitkb.analysis.engine import StaticAnalysisEngine


def test_kotlin_constructor_properties_keep_nullability_defaults_and_validation(tmp_path):
    (tmp_path / "Controller.kt").write_text('''
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody

data class CreateRequest(
    @field:NotBlank val title: String,
    val quantities: Map<String, List<Int>> = emptyMap(),
    var note: String? = null,
    trace: String
) {
    val computed: String = "internal"
}

class Controller {
    @PostMapping("/orders")
    fun create(@RequestBody request: CreateRequest): String = request.title
}
''')

    analysis = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert analysis.contracts["Controller.create"]["request"]["fields"] == [
        {"name": "title", "type": "String", "required": True, "validations": ["NotBlank"]},
        {"name": "quantities", "type": "Map<String, List<Int>>", "required": False, "validations": []},
        {"name": "note", "type": "String", "required": False, "validations": []},
    ]
