package com.example.sampleorder.application.web.controller.output

import com.example.sampleorder.domain.model.entity.Bill
import ulid.ULID

data class BillOut(val billId: ULID, val orderCount: Int, val requestedBy: ULID)
fun Bill.resumeOut(userId: ULID) = BillOut(id, orders.size, userId)
