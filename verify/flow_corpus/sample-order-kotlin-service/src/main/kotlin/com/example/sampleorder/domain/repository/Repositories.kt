package com.example.sampleorder.domain.repository

import com.example.sampleorder.domain.model.entity.Bill
import com.example.sampleorder.domain.model.entity.BillItem
import com.example.sampleorder.domain.model.entity.Order
import java.util.Optional
import ulid.ULID

interface BillRepository {
    fun findByIdAndTableIdAndTableRestaurantId(id: ULID, tableId: ULID, restaurantId: ULID): Optional<Bill>
}

interface BillOrderRepository {
    fun add(bill: Bill, orderId: ULID, item: BillItem)
    fun add(bill: Bill, order: Order)
}
