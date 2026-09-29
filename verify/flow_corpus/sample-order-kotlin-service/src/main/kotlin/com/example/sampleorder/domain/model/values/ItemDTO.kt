package com.example.sampleorder.domain.model.values

import ulid.ULID

data class IngredientDTO(val id: ULID, val quantity: Int)
data class ItemDTO(
    val id: ULID,
    val menuId: ULID,
    val annotation: String,
    val ingredientsAdded: List<IngredientDTO>,
    val ingredientsRemoved: List<IngredientDTO>
) {
    fun hasChange() = ingredientsAdded.isNotEmpty() || hasRemovedIngredients()
    fun hasRemovedIngredients() = ingredientsRemoved.isNotEmpty()
}
