package com.example.sampleorder.domain.model.entity

import ulid.ULID
import org.springframework.data.mongodb.core.mapping.Document
import java.math.BigDecimal

data class Ingredient(val id: ULID, val name: String, val quantity: Int, val addOn: Boolean = false) {
    fun removes(amount: Int) = copy(quantity = quantity - amount)
}

data class Item(
    val id: ULID,
    val name: String,
    val ingredients: List<Ingredient>,
    val price: BigDecimal
)
data class BillItem(val id: ULID, val item: Item, val annotation: String)
data class Order(
    val id: ULID,
    val customerId: ULID,
    val items: List<BillItem>,
    val customerName: String? = null,
    val open: Boolean = true
) {
    fun isOpen() = open
}

@Document("checks")
data class Bill(val id: ULID, val orders: List<Order>, val open: Boolean = true) {
    fun isOpen() = open
    fun add(customerId: ULID, item: BillItem, customerName: String?): Bill {
        val order = orders.firstOrNull { it.isOpen() && it.customerId == customerId }
        return if (order == null) copy(orders = orders + Order(item.id, customerId, listOf(item), customerName))
        else copy(orders = orders.map { if (it.id == order.id) it.copy(items = it.items + item) else it })
    }
}
