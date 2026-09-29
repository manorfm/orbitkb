package com.example.sampleorder

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication
import org.springframework.cloud.openfeign.EnableFeignClients
import org.springframework.amqp.rabbit.annotation.EnableRabbit

@SpringBootApplication
@EnableFeignClients
@EnableRabbit
class Application

fun main(args: Array<String>) {
    runApplication<Application>(*args)
}
