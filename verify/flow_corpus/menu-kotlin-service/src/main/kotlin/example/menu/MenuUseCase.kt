package example.menu

class MenuUseCase(private val menuGateway: MenuGateway) {
    fun get(id: String): Menu = menuGateway.fetch(id)
}
