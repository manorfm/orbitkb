package com.example.sampleorder.resources.infra.security

import org.springframework.core.convert.converter.Converter
import org.springframework.security.authentication.AbstractAuthenticationToken
import org.springframework.security.oauth2.jwt.Jwt
import org.springframework.security.oauth2.server.resource.authentication.JwtAuthenticationToken
import org.springframework.security.core.authority.SimpleGrantedAuthority

class JWTConverter : Converter<Jwt, AbstractAuthenticationToken> {
    override fun convert(jwt: Jwt): AbstractAuthenticationToken {
        val roles = jwt.claims["roles"] as List<String>
        val authorities = roles.map { SimpleGrantedAuthority("ROLE_${it.uppercase()}") }
        return JwtAuthenticationToken(jwt, authorities)
    }
}
