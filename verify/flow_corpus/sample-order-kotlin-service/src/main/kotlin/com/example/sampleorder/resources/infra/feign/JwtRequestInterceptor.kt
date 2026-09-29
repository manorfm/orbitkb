package com.example.sampleorder.resources.infra.feign

import feign.RequestInterceptor
import feign.RequestTemplate
import org.springframework.security.core.context.SecurityContextHolder
import org.springframework.security.oauth2.jwt.Jwt
import org.springframework.stereotype.Component

@Component
class JwtRequestInterceptor(private val serviceTokenProvider: ServiceTokenProvider) : RequestInterceptor {
    override fun apply(template: RequestTemplate) {
        val jwt = SecurityContextHolder.getContext().authentication?.credentials as? Jwt
        val token = jwt?.tokenValue ?: serviceTokenProvider.getToken()
        template.header("Authorization", "Bearer $token")
    }
}
