package com.example.sampleorder.resources.provider.client

import org.springframework.cloud.openfeign.FeignClient
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import java.math.BigDecimal
import ulid.ULID

data class IngredientResponse(val id: ULID, val name: String, val quantity: Int)
data class ItemResponse(
    val id: ULID,
    val name: String,
    val ingredients: List<IngredientResponse>,
    val price: BigDecimal
)

@FeignClient(name = "catalog-service", url = "\${provider.catalog-service.url}")
interface MenuClient {
    @GetMapping("/venues/{restaurantId}/catalogs/{menuId}/products/{itemId}/summary")
    fun getItem(
        @PathVariable restaurantId: ULID,
        @PathVariable menuId: ULID,
        @PathVariable itemId: ULID
    ): ItemResponse

    @GetMapping("/venues/{restaurantId}/ingredients/{ingredientId}")
    fun getIngredient(
        @PathVariable restaurantId: ULID,
        @PathVariable ingredientId: ULID
    ): IngredientResponse
}
