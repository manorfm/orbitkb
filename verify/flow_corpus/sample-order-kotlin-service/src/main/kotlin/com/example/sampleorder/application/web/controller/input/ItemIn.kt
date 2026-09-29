package com.example.sampleorder.application.web.controller.input

import com.example.sampleorder.domain.model.values.IngredientDTO
import com.example.sampleorder.domain.model.values.ItemDTO
import org.springframework.security.oauth2.jwt.Jwt
import ulid.ULID

data class IngredientIn(val id: ULID, val quantity: Int)
data class ItemIn(
    val id: ULID,
    val menuId: ULID,
    val annotation: String? = null,
    val ingredientsAdded: List<IngredientIn>? = null,
    val ingredientsRemoved: List<IngredientIn>? = null
)

fun Jwt.getUserId(): ULID = ULID.parseULID(claims["sub"] as String)
fun IngredientIn.toDTO() = IngredientDTO(id, quantity)
fun ItemIn.toDTO() = ItemDTO(
    id, menuId, annotation.orEmpty(),
    ingredientsAdded.orEmpty().map { it.toDTO() },
    ingredientsRemoved.orEmpty().map { it.toDTO() }
)
