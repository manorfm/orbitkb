package com.deskordering.application.web.controller.out

import com.deskordering.domain.Restaurant

fun Restaurant.out() = RestaurantOut(
    id = id,
    name = name,
)

data class RestaurantOut(
    val id: String,
    val name: String,
)
