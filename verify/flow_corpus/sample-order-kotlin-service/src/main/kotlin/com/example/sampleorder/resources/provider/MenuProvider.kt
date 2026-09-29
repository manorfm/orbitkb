package com.example.sampleorder.resources.provider

import com.example.sampleorder.domain.gateway.MenuGateway
import com.example.sampleorder.domain.model.entity.Ingredient
import com.example.sampleorder.domain.model.entity.Item
import com.example.sampleorder.resources.provider.client.MenuClient
import org.springframework.stereotype.Component
import ulid.ULID

@Component
class MenuProvider(private val menuClient: MenuClient) : MenuGateway {
    override fun getItem(restaurantId: ULID, menuId: ULID, itemId: ULID): Item {
        val response = menuClient.getItem(restaurantId, menuId, itemId)
        return Item(
            response.id,
            response.name,
            response.ingredients.map { Ingredient(it.id, it.name, it.quantity) },
            response.price
        )
    }

    override fun getIngredient(restaurantId: ULID, ingredientId: ULID): Ingredient {
        val response = menuClient.getIngredient(restaurantId, ingredientId)
        return Ingredient(response.id, response.name, response.quantity)
    }
}
