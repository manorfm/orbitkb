package com.example.sampleorder.resources.infra.feign

import org.springframework.security.oauth2.client.OAuth2AuthorizeRequest
import org.springframework.security.oauth2.client.OAuth2AuthorizedClientManager
import org.springframework.stereotype.Component

interface ServiceTokenProvider {
    fun getToken(): String
}

@Component
class OAuth2ServiceTokenProvider(
    private val authorizedClientManager: OAuth2AuthorizedClientManager
) : ServiceTokenProvider {
    override fun getToken(): String {
        val request = OAuth2AuthorizeRequest.withClientRegistrationId("sample-order-service")
            .principal("sample-order-service").build()
        val client = authorizedClientManager.authorize(request)
            ?: throw IllegalStateException("service client authorization failed")
        return client.accessToken.tokenValue
    }
}
