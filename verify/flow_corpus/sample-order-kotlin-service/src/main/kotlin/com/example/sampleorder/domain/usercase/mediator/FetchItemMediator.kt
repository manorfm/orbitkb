package com.example.sampleorder.domain.usercase.mediator

import com.example.sampleorder.domain.gateway.MenuGateway
import com.example.sampleorder.domain.model.entity.Item
import com.example.sampleorder.domain.model.values.ItemDTO
import org.springframework.stereotype.Component
import ulid.ULID

@Component
class FetchItemMediator(private val menuGateway: MenuGateway) {
    fun get(restaurantId: ULID, itemDTO: ItemDTO): Item {
        val item = menuGateway.getItem(restaurantId, itemDTO.menuId, itemDTO.id)
        if (!itemDTO.hasChange()) return item
        val removed = itemDTO.ingredientsRemoved.associateBy { it.id }
        val existing = item.ingredients.mapNotNull { ingredient ->
            val change = removed[ingredient.id] ?: return@mapNotNull ingredient
            val updated = ingredient.removes(change.quantity)
            if (updated.quantity > 0 || (updated.quantity == 0 && !ingredient.addOn)) updated else null
        }
        val added = itemDTO.ingredientsAdded.map { change ->
            menuGateway.getIngredient(restaurantId, change.id).copy(quantity = change.quantity)
        }
        return item.copy(ingredients = existing + added)
    }
}
