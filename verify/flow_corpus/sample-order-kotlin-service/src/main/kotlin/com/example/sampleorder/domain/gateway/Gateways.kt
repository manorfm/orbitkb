package com.example.sampleorder.domain.gateway

import com.example.sampleorder.domain.model.entity.Ingredient
import com.example.sampleorder.domain.model.entity.Item
import ulid.ULID

interface MenuGateway {
    fun getItem(restaurantId: ULID, menuId: ULID, itemId: ULID): Item
    fun getIngredient(restaurantId: ULID, ingredientId: ULID): Ingredient
}

data class UserInfo(val name: String)
interface UserGateway {
    fun getUserInfo(): UserInfo
}
