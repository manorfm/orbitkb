package com.example.sampleorder.resources.producer

import com.example.sampleorder.application.web.sse.TableSseEvent
import com.example.sampleorder.domain.model.entity.BillItem
import com.example.sampleorder.domain.model.entity.Order
import com.example.sampleorder.domain.notification.OrderLifecycleNotification
import org.springframework.data.redis.core.StringRedisTemplate
import org.springframework.stereotype.Component
import tools.jackson.databind.json.JsonMapper
import ulid.ULID

@Component
class OrderLifecycleEventProducer(
    private val redisTemplate: StringRedisTemplate,
    private val jsonMapper: JsonMapper
) : OrderLifecycleNotification {
    override fun itemAdded(tableId: ULID, billId: ULID, order: Order, item: BillItem) {
        val event = TableSseEvent.ItemAdded(
            billId = billId,
            orderId = order.id,
            itemId = item.id,
            itemName = item.item.name,
            customerName = order.customerName,
            customerId = order.customerId,
            price = item.item.price
        )
        val json = jsonMapper.writeValueAsString(event)
        redisTemplate.convertAndSend("events:spot:$tableId", json)
    }
}
