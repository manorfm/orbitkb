package example.menu

import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

@RestController
@RequestMapping(
    "/menus",
    produces = ["application/json"]
)
class MenuController(private val menuUseCase: MenuQuery) {
    @GetMapping("/{id}")
    fun get(@PathVariable id: String): Menu = menuUseCase.get(id)

    @GetMapping("/by-restaurant/{id}")
    fun byRestaurant(@PathVariable id: String): Menu = menuUseCase.get(id)
}
