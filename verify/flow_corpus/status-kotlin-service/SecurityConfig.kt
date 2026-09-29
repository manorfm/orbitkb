class SecurityConfig {
    fun filterChain(http: HttpSecurity): SecurityFilterChain {
        http {
            authorizeHttpRequests {
                authorize(HttpMethod.GET, "/status", permitAll)
            }
        }
        return http.build()
    }
}
