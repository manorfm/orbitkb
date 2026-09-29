package com.example.sampleorder.application.web.sse

import java.math.BigDecimal
import java.time.Instant
import ulid.ULID

sealed class TableSseEvent {
    data class ItemAdded(
        val billId: ULID,
        val orderId: ULID,
        val itemId: ULID,
        val itemName: String,
        val customerName: String?,
        val customerId: ULID,
        val price: BigDecimal,
        val timestamp: Instant = Instant.now()
    ) : TableSseEvent()
}
