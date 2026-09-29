data class Status(val value: String)

@RestController
class StatusController {
    @GetMapping("/status")
    fun get(): Status = Status("ok")
}
