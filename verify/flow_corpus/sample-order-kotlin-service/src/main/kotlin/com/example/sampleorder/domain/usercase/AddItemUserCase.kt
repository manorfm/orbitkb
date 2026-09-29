package com.example.sampleorder.domain.usercase

import com.example.sampleorder.domain.command.AddItemCommand
import com.example.sampleorder.domain.gateway.UserGateway
import com.example.sampleorder.domain.model.entity.Bill
import com.example.sampleorder.domain.model.entity.BillItem
import com.example.sampleorder.domain.model.values.ItemDTO
import com.example.sampleorder.domain.notification.OrderLifecycleNotification
import com.example.sampleorder.domain.services.BillOrderService
import com.example.sampleorder.domain.services.BillService
import com.example.sampleorder.domain.usercase.mediator.FetchItemMediator
import org.springframework.stereotype.Service
import ulid.ULID

@Service
class AddItemUserCase(
    private val billService: BillService,
    private val billOrderService: BillOrderService,
    private val fetchItemMediator: FetchItemMediator,
    private val userGateway: UserGateway,
    private val orderLifecycleNotification: OrderLifecycleNotification
) : AddItemCommand {
    override fun add(restaurantId: ULID, tableId: ULID, billId: ULID, customerId: ULID, itemDTO: ItemDTO): Bill {
        val bill = billService.get(restaurantId, tableId, billId)
        if (!bill.isOpen()) throw IllegalStateException("bill is closed")
        val item = fetchItemMediator.get(restaurantId, itemDTO)
        val billItem = BillItem(id = itemDTO.id, item = item, annotation = itemDTO.annotation)
        val customerName = runCatching { userGateway.getUserInfo().name }.getOrNull()
        val updated = billOrderService.addItem(bill, customerId, billItem, customerName)
        val order = updated.orders.first { candidate -> candidate.items.any { it.id == billItem.id } }
        orderLifecycleNotification.itemAdded(tableId, billId, order, billItem)
        return updated
    }
}
