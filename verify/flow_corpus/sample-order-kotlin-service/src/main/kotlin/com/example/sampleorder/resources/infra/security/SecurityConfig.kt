package com.example.sampleorder.resources.infra.security

import org.springframework.context.annotation.Bean
import org.springframework.context.annotation.Configuration
import org.springframework.http.HttpMethod
import org.springframework.security.config.annotation.web.builders.HttpSecurity
import org.springframework.security.config.annotation.web.invoke
import org.springframework.security.web.SecurityFilterChain

@Configuration
class SecurityConfig {
    @Bean
    fun filterChain(http: HttpSecurity): SecurityFilterChain {
        http {
            oauth2ResourceServer { jwt { jwtAuthenticationConverter = JWTConverter() } }
            authorizeHttpRequests {
                authorize(HttpMethod.GET, "/venues/*/spots/*/checks/*", permitAll)
                authorize(anyRequest, authenticated)
            }
        }
        return http.build()
    }
}
