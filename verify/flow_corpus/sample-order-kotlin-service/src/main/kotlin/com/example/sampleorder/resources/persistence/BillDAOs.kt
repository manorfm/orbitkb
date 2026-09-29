package com.example.sampleorder.resources.persistence

import com.example.sampleorder.domain.model.entity.Bill
import com.example.sampleorder.domain.model.entity.BillItem
import com.example.sampleorder.domain.model.entity.Order
import com.example.sampleorder.domain.repository.BillOrderRepository
import com.example.sampleorder.domain.repository.BillRepository
import org.springframework.data.mongodb.core.MongoTemplate
import org.springframework.data.mongodb.repository.MongoRepository
import org.springframework.stereotype.Repository
import com.mongodb.client.model.UpdateOptions
import org.bson.Document
import ulid.ULID

@Repository
interface BillDAO : BillRepository, MongoRepository<Bill, ULID>

@Repository
class BillOrderDAO(private val mongoTemplate: MongoTemplate) : BillOrderRepository {
    override fun add(bill: Bill, orderId: ULID, item: BillItem) {
        mongoTemplate.execute(Bill::class.java) { collection ->
            val itemDoc = Document()
            mongoTemplate.converter.write(item, itemDoc)
            val update = Document("\$push", Document("orders.\$[order].items", itemDoc))
            val options = UpdateOptions().arrayFilters(listOf(Document("order._id", orderId.toString())))
            collection.updateOne(Document("_id", bill.id.toString()), update, options)
        }
    }

    override fun add(bill: Bill, order: Order) {
        mongoTemplate.execute(Bill::class.java) { collection ->
            val orderDoc = Document()
            mongoTemplate.converter.write(order, orderDoc)
            collection.updateOne(
                Document("_id", bill.id.toString()),
                Document("\$push", Document("orders", orderDoc))
            )
        }
    }
}
