package com.example.sampleorder.domain.services

import com.example.sampleorder.domain.model.entity.Bill
import com.example.sampleorder.domain.model.entity.BillItem
import com.example.sampleorder.domain.model.entity.Order
import com.example.sampleorder.domain.repository.BillOrderRepository
import org.springframework.stereotype.Service
import ulid.ULID

@Service
class BillOrderService(private val billOrderRepository: BillOrderRepository) {
    fun addItem(bill: Bill, customerId: ULID, item: BillItem, customerName: String?): Bill {
        val updated = bill.add(customerId, item, customerName)
        val order: Order = updated.orders.first { it.isOpen() && it.customerId == customerId }
        if (bill.orders.size == updated.orders.size) {
            billOrderRepository.add(updated, order.id, item)
        } else {
            billOrderRepository.add(updated, order)
        }
        return updated
    }
}
