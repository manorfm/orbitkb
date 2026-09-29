package example.menu

import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

@RestController
@RequestMapping("/menus")
class MenuController(private val menuUseCase: MenuUseCase) {
    @GetMapping("/{id}")
    fun get(@PathVariable id: String): Menu = menuUseCase.get(id)
}
