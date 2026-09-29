package example.menu

class MenuGateway(private val restaurantClient: RestaurantClient) {
    fun fetch(id: String): Menu {
        val restaurant = restaurantClient.getRestaurant(id)
        return Menu(id, restaurant.name)
    }
}

data class Menu(val id: String, val restaurantName: String)
