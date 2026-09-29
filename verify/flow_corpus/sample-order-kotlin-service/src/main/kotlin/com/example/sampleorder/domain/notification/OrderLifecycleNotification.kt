package com.example.sampleorder.domain.notification

import com.example.sampleorder.domain.model.entity.BillItem
import com.example.sampleorder.domain.model.entity.Order
import ulid.ULID

interface OrderLifecycleNotification {
    fun itemAdded(tableId: ULID, billId: ULID, order: Order, item: BillItem)
}
