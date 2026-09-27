package com.deskordering.application.web.controller

import com.deskordering.application.web.controller.out.out
import com.deskordering.domain.services.RestaurantService
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

@RestController
@RequestMapping("/restaurants")
class RestaurantController(
    private val restaurantService: RestaurantService,
) {
    @GetMapping("/{id}")
    fun get(@PathVariable id: String) =
        restaurantService.get(id).out()
}
