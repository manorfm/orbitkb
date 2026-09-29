package example.menu

interface MenuQuery {
    fun get(id: String): Menu
}

class MenuUseCase(private val menuGateway: RestaurantGateway) : MenuQuery {
    override fun get(id: String): Menu = menuGateway.fetch(id)
}
