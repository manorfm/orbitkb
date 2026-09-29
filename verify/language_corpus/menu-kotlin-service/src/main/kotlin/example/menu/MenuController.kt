package example.menu

import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

data class Menu(val id: String, val name: String)
data class CreateMenuRequest(val name: String)

@RestController
@RequestMapping("/menus")
class MenuController {
    @GetMapping("/{id}")
    fun get(@PathVariable id: String): Menu = Menu(id, "sample")

    @PostMapping
    fun create(@RequestBody request: CreateMenuRequest): Menu = Menu("created", request.name)
}
