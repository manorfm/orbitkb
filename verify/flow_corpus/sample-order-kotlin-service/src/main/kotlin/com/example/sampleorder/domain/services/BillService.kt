package com.example.sampleorder.domain.services

import com.example.sampleorder.domain.model.entity.Bill
import com.example.sampleorder.domain.repository.BillRepository
import org.springframework.stereotype.Service
import ulid.ULID

@Service
class BillService(private val billRepository: BillRepository) {
    fun get(restaurantId: ULID, tableId: ULID, billId: ULID): Bill =
        billRepository.findByIdAndTableIdAndTableRestaurantId(billId, tableId, restaurantId)
            .orElseThrow { NoSuchElementException("bill not found") }
}
