package example.menu

import org.springframework.cloud.openfeign.FeignClient
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable

@FeignClient(name = "restaurant-service")
interface RestaurantClient {
    @GetMapping("/restaurants/{id}")
    fun getRestaurant(@PathVariable id: String): Restaurant
}

data class Restaurant(val name: String)
