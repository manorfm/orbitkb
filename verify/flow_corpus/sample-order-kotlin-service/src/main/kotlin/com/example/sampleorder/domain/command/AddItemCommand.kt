package com.example.sampleorder.domain.command

import com.example.sampleorder.domain.model.entity.Bill
import com.example.sampleorder.domain.model.values.ItemDTO
import ulid.ULID

interface AddItemCommand {
    fun add(restaurantId: ULID, tableId: ULID, billId: ULID, customerId: ULID, itemDTO: ItemDTO): Bill
}
