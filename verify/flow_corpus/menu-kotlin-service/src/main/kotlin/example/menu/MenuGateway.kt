package example.menu

interface RestaurantGateway {
    fun fetch(id: String): Menu
}

class MenuGateway(private val restaurantClient: RestaurantClient) : RestaurantGateway {
    override fun fetch(id: String): Menu {
        val restaurant = restaurantClient.getRestaurant(id)
        return Menu(id, restaurant.name)
    }
}

data class Menu(val id: String, val restaurantName: String)
