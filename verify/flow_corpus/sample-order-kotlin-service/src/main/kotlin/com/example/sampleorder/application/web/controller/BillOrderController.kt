package com.example.sampleorder.application.web.controller

import com.example.sampleorder.application.web.controller.input.ItemIn
import com.example.sampleorder.application.web.controller.input.getUserId
import com.example.sampleorder.application.web.controller.input.toDTO
import com.example.sampleorder.application.web.controller.output.resumeOut
import com.example.sampleorder.domain.command.AddItemCommand
import org.springframework.http.HttpStatus
import org.springframework.security.core.annotation.AuthenticationPrincipal
import org.springframework.security.oauth2.jwt.Jwt
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.PostMapping
import org.springframework.web.bind.annotation.RequestBody
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.ResponseStatus
import org.springframework.web.bind.annotation.RestController
import ulid.ULID

@RestController
@RequestMapping(
    "/venues/{restaurantId}/spots/{tableId}/checks/{billId}",
    produces = ["application/json"], consumes = ["application/json"]
)
class BillOrderController(private val addItemCommand: AddItemCommand) {
    @PostMapping("/items")
    @ResponseStatus(HttpStatus.CREATED)
    fun addItem(
        @AuthenticationPrincipal jwt: Jwt,
        @PathVariable restaurantId: ULID,
        @PathVariable tableId: ULID,
        @PathVariable billId: ULID,
        @RequestBody itemIn: ItemIn
    ) = addItemCommand.add(restaurantId, tableId, billId, jwt.getUserId(), itemIn.toDTO())
        .resumeOut(jwt.getUserId())
}
