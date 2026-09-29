package com.example.sampleorder.resources.infra.feign

import feign.Response
import feign.codec.ErrorDecoder
import java.nio.charset.StandardCharsets

class FeignErrorDecoder : ErrorDecoder {
    override fun decode(methodKey: String, response: Response): Exception {
        val body = response.body()?.asReader(StandardCharsets.UTF_8)?.readText().orEmpty()
        return when (response.status()) {
            400 -> IllegalArgumentException("menu request rejected")
            404 -> NoSuchElementException("menu item not found")
            else -> IllegalStateException("menu service failed; response size=${body.length}")
        }
    }
}
