from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine


def test_go_analyzer_maps_route_to_internal_and_persistence_flow(tmp_path: Path):
    source = tmp_path / "main.go"
    source.write_text(
        '''package main
type Orders struct{}
func (o *Orders) Create() { o.useCase.Execute(); o.repo.Save() }
func main() { router.POST("/orders", orders.Create) }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    entry = result.entrypoints[0]
    assert (entry.kind, entry.method, entry.name) == ("http", "POST", "/orders")
    assert entry.symbol.endswith("Create")
    assert {(edge.kind, edge.target) for edge in result.edges} >= {
        ("invokes", "o.useCase.Execute"),
        ("writes", "o.repo.Save"),
    }


def test_go_analyzer_classifies_explicit_gorm_database_operations(tmp_path: Path):
    (tmp_path / "orders.go").write_text(
        '''package orders
func FindOrder(db *gorm.DB, id string) { db.First(&Order{}, id) }
func CreateOrder(db *gorm.DB, order Order) { db.Create(&order) }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("orders.FindOrder", "db.First", "reads"),
        ("orders.CreateOrder", "db.Create", "writes"),
    }


def test_go_analyzer_classifies_simple_gorm_fluent_operations(tmp_path: Path):
    (tmp_path / "orders.go").write_text(
        '''package orders
func FindOrder(db *gorm.DB, status string) { db.Where("status = ?", status).First(&Order{}) }
func CreateOrder(ctx context.Context, db *gorm.DB, order Order) { db.WithContext(ctx).Create(&order) }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("orders.FindOrder", 'db.Where("status = ?", status).First', "reads"),
        ("orders.CreateOrder", "db.WithContext(ctx).Create", "writes"),
    }


def test_go_analyzer_classifies_explicit_database_sql_operations(tmp_path: Path):
    (tmp_path / "orders.go").write_text(
        '''package orders
func FindOrder(db *sql.DB, id string) { return db.QueryRowContext(ctx, "select id from orders where id = ?", id) }
func CreateOrder(tx *sql.Tx, id string) { tx.ExecContext(ctx, "insert into orders(id) values(?)", id) }
func Unproven(client Client, id string) { client.ExecContext(ctx, "insert into orders(id) values(?)", id) }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("orders.FindOrder", "db.QueryRowContext", "reads"),
        ("orders.CreateOrder", "tx.ExecContext", "writes"),
        ("orders.Unproven", "client.ExecContext", "invokes"),
    }


def test_kotlin_spring_analyzer_finds_constructor_injection_and_route(tmp_path: Path):
    source = tmp_path / "OrdersController.kt"
    source.write_text(
        '''@RestController
class OrdersController(private val useCase: CreateOrderUseCase) {
  @PostMapping("/orders")
  fun create(request: OrderRequest) = useCase.execute(request)
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(entry.method, entry.name) for entry in result.entrypoints] == [("POST", "/orders")]
    assert any(edge.kind == "injects" and edge.target == "CreateOrderUseCase" for edge in result.edges)
    assert any(edge.kind == "invokes" and edge.target == "useCase.execute" for edge in result.edges)


def test_jvm_spring_analyzer_extracts_a_pre_authorize_role_requirement(tmp_path: Path):
    (tmp_path / "OrdersController.kt").write_text(
        '''@RestController
class OrdersController(private val useCase: CancelOrderUseCase) {
  @PreAuthorize("hasRole('ADMIN')")
  @DeleteMapping("/orders/{id}")
  fun cancel(@PathVariable id: String) = useCase.execute(id)
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(r.symbol, r.requirement, r.roles) for r in result.security_requirements] == [
        ("OrdersController.cancel", "hasRole", ("ADMIN",)),
    ]


def test_java_spring_analyzer_extracts_a_secured_requirement(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''class OrdersController {
  @Secured("ROLE_ADMIN")
  @DeleteMapping("/orders/{id}")
  public void cancel(String id) {
    useCase.execute(id);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(r.symbol, r.requirement, r.roles) for r in result.security_requirements] == [
        ("OrdersController.cancel", "secured", ("ROLE_ADMIN",)),
    ]


def test_jvm_spring_analyzer_extracts_a_filter_chain_route_requirement(tmp_path: Path):
    (tmp_path / "SecurityConfig.kt").write_text(
        '''@Configuration
class SecurityConfig {
    @Bean
    fun filterChain(http: HttpSecurity): SecurityFilterChain {
        http {
            authorizeHttpRequests {
                authorize(HttpMethod.POST, "/orders/{id}/cancel", hasRole("ADMIN"))
                authorize(anyRequest, authenticated)
            }
        }
        return http.build()
    }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(r.route_pattern, r.method, r.requirement, r.roles) for r in result.security_requirements] == [
        ("/orders/{id}/cancel", "POST", "hasRole", ("ADMIN",)),
        ("**", None, "authenticated", ()),
    ]


def test_jvm_spring_analyzer_links_feign_invocation_with_a_path_parameter_in_the_mapping(tmp_path: Path):
    """The Feign interface body used to be captured with a naive `.*?}` regex, which
    stopped at the FIRST `}` anywhere in the body -- including one inside a route's
    own `{id}` path-parameter placeholder, truncating the body before the real
    closing brace and silently losing every mapping in it. Found tracing a real
    endpoint whose Feign client mapping is `@GetMapping("/restaurants/{id}", ...)`.
    """
    (tmp_path / "RestaurantClient.kt").write_text(
        '''@FeignClient("restaurant", url = $$"${provider.restaurant-client.url}")
interface RestaurantClient {
    @GetMapping("/restaurants/{id}", consumes = [MediaType.APPLICATION_JSON_VALUE])
    fun getRestaurant(@PathVariable("id") id: ULID): RestaurantResponse
}
''',
        encoding="utf-8",
    )
    (tmp_path / "RestaurantProvider.kt").write_text(
        '''class RestaurantProvider(private val restaurantClient: RestaurantClient) {
    fun getRestaurant(id: ULID): Restaurant {
        return restaurantClient.getRestaurant(id).toDomain()
    }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.source, call.target_service, call.protocol, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("RestaurantProvider.getRestaurant", "restaurant", "http", "GET", "/restaurants/{id}"),
    ]


def test_jvm_spring_analyzer_links_feign_invocation_to_declared_target_endpoint(tmp_path: Path):
    (tmp_path / "InventoryClient.java").write_text(
        '''@FeignClient(name = "inventory")
interface InventoryClient {
  @PostMapping("/reservations")
  Reservation reserve(ReserveRequest request);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.java").write_text(
        '''class CheckoutService {
  private InventoryClient inventoryClient;
  Receipt checkout(ReserveRequest request) {
    Reservation reservation = inventoryClient.reserve(request);
    return new Receipt(reservation);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.source, call.target_service, call.protocol, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("CheckoutService.checkout", "inventory", "http", "POST", "/reservations"),
    ]


def test_jvm_spring_analyzer_links_kotlin_feign_invocation_to_declared_target_endpoint(tmp_path: Path):
    (tmp_path / "InventoryClient.kt").write_text(
        '''@FeignClient(name = "inventory")
interface InventoryClient {
  @PostMapping("/reservations")
  fun reserve(request: ReserveRequest): Reservation
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.kt").write_text(
        '''class CheckoutService(private val inventoryClient: InventoryClient) {
  fun checkout(request: ReserveRequest): Receipt {
    val reservation = inventoryClient.reserve(request)
    return Receipt(reservation)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.source, call.target_service, call.protocol, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("CheckoutService.checkout", "inventory", "http", "POST", "/reservations"),
    ]


def test_jvm_spring_analyzer_composes_a_feign_route_prefix(tmp_path: Path):
    (tmp_path / "InventoryClient.java").write_text(
        '''@FeignClient(name = "inventory")
@RequestMapping("/v1")
interface InventoryClient {
  @PostMapping("/reservations")
  Reservation reserve(ReserveRequest request);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.java").write_text(
        '''class CheckoutService {
  private InventoryClient inventoryClient;
  Receipt checkout(ReserveRequest request) {
    return new Receipt(inventoryClient.reserve(request));
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.target_service, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("inventory", "POST", "/v1/reservations"),
    ]


def test_jvm_spring_analyzer_links_a_positional_feign_client_invocation(tmp_path: Path):
    """`@FeignClient("inventory", url = ...)` -- the service name given positionally,
    no `name=`/`value=` keyword -- is valid Spring syntax the pattern didn't
    recognize (found tracing a real endpoint in a Kotlin/Spring service that uses
    exactly this style for both of its Feign clients).
    """
    (tmp_path / "InventoryClient.kt").write_text(
        '''@FeignClient("inventory", url = $$"${provider.inventory-client.url}")
interface InventoryClient {
  @PostMapping("/reservations")
  fun reserve(request: ReserveRequest): Reservation
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.kt").write_text(
        '''class CheckoutService(private val inventoryClient: InventoryClient) {
  fun checkout(request: ReserveRequest): Receipt {
    val reservation = inventoryClient.reserve(request)
    return Receipt(reservation)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.source, call.target_service, call.protocol, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("CheckoutService.checkout", "inventory", "http", "POST", "/reservations"),
    ]


def test_jvm_spring_analyzer_ignores_a_dynamic_positional_feign_client_name(tmp_path: Path):
    """A non-literal positional name (a constant reference, not a string literal)
    must not become a fact -- same "only a literal proves it" posture as the
    keyword form already has.
    """
    (tmp_path / "InventoryClient.kt").write_text(
        '''@FeignClient(INVENTORY_SERVICE_NAME)
interface InventoryClient {
  @PostMapping("/reservations")
  fun reserve(request: ReserveRequest): Reservation
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.kt").write_text(
        '''class CheckoutService(private val inventoryClient: InventoryClient) {
  fun checkout(request: ReserveRequest): Receipt {
    val reservation = inventoryClient.reserve(request)
    return Receipt(reservation)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert result.static_service_calls == []


def test_jvm_spring_analyzer_extracts_a_feign_client_url_as_a_configuration_binding(tmp_path: Path):
    """A Feign client's `url` attribute names the actual config key the client's
    base URL comes from -- as real a configuration dependency as an `@Value`
    field, but never modeled as one until now (found tracing the same real
    Kotlin/Spring service as the positional-syntax gap above, which spells this
    exact placeholder with Kotlin's multi-dollar string literal: `$$"${...}"`).
    """
    (tmp_path / "InventoryClient.kt").write_text(
        '''@FeignClient("inventory", url = $$"${provider.inventory-client.url}")
interface InventoryClient {
  @PostMapping("/reservations")
  fun reserve(request: ReserveRequest): Reservation
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(b.source, b.key, b.kind, b.sensitive) for b in result.configuration_bindings] == [
        ("InventoryClient", "provider.inventory-client.url", "property", False),
    ]


def test_jvm_spring_analyzer_extracts_a_feign_client_url_written_with_a_backslash_escape(tmp_path: Path):
    """Kotlin's older escaping style for the same fact: `"\\${key}"` (a literal
    backslash before the `$`, so the compiler doesn't parse `${...}` as string-
    template interpolation) -- must resolve to the same key as the plain and
    multi-dollar forms.
    """
    (tmp_path / "InventoryClient.kt").write_text(
        '''@FeignClient("inventory", url = "\\${provider.inventory-client.url}")
interface InventoryClient {
  @PostMapping("/reservations")
  fun reserve(request: ReserveRequest): Reservation
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(b.source, b.key) for b in result.configuration_bindings] == [
        ("InventoryClient", "provider.inventory-client.url"),
    ]


def test_jvm_spring_analyzer_ignores_a_feign_client_with_no_url_attribute(tmp_path: Path):
    (tmp_path / "InventoryClient.java").write_text(
        '''@FeignClient(name = "inventory")
interface InventoryClient {
  @PostMapping("/reservations")
  Reservation reserve(ReserveRequest request);
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert result.configuration_bindings == []


def test_jvm_spring_analyzer_links_injected_rest_template_to_a_literal_service_endpoint(tmp_path: Path):
    (tmp_path / "CheckoutService.java").write_text(
        '''class CheckoutService {
  private RestTemplate restTemplate;
  Receipt checkout(ReserveRequest request) {
    Reservation reservation = restTemplate.postForEntity(
        "http://inventory/reservations", request, Reservation.class).getBody();
    return new Receipt(reservation);
  }
  Receipt unproven(ReserveRequest request) {
    return new HttpClient().postForEntity("http://payments/charges", request, Receipt.class);
  }
  Receipt external(ReserveRequest request) {
    return restTemplate.postForEntity("https://api.stripe.com/charges?token=ignored", request, Receipt.class);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.source, call.target_service, call.protocol, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("CheckoutService.checkout", "inventory", "http", "POST", "/reservations"),
    ]


def test_jvm_spring_analyzer_links_kotlin_injected_rest_template_to_a_literal_service_endpoint(tmp_path: Path):
    (tmp_path / "CheckoutService.kt").write_text(
        '''class CheckoutService(private val restTemplate: RestTemplate) {
  fun checkout(request: ReserveRequest): Receipt {
    val reservation = restTemplate.postForEntity(
      "http://inventory/reservations", request, Reservation::class.java).body
    return Receipt(reservation)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.source, call.target_service, call.protocol, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("CheckoutService.checkout", "inventory", "http", "POST", "/reservations"),
    ]


def test_jvm_spring_analyzer_links_injected_web_client_to_a_literal_service_endpoint(tmp_path: Path):
    (tmp_path / "CheckoutService.java").write_text(
        '''class CheckoutService {
  private WebClient webClient;
  Receipt checkout(ReserveRequest request) {
    return webClient.post().uri("http://inventory/reservations").retrieve().bodyToMono(Receipt.class).block();
  }
  Receipt unproven(ReserveRequest request) {
    return WebClient.create().post().uri("http://payments/charges").retrieve().bodyToMono(Receipt.class).block();
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.source, call.target_service, call.protocol, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("CheckoutService.checkout", "inventory", "http", "POST", "/reservations"),
    ]


def test_jvm_spring_analyzer_links_kotlin_injected_web_client_to_a_literal_service_endpoint(tmp_path: Path):
    (tmp_path / "CheckoutService.kt").write_text(
        '''class CheckoutService(private val webClient: WebClient) {
  fun checkout(): Receipt {
    return webClient.get().uri("http://inventory/reservations").retrieve().bodyToMono(Receipt::class.java).block()!!
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.source, call.target_service, call.protocol, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("CheckoutService.checkout", "inventory", "http", "GET", "/reservations"),
    ]


def test_jvm_spring_analyzer_links_literal_http_methods_in_rest_template_and_web_client(tmp_path: Path):
    (tmp_path / "CheckoutService.java").write_text(
        '''class CheckoutService {
  private RestTemplate restTemplate;
  private WebClient webClient;
  Receipt reconcile(ReserveRequest request) {
    return restTemplate.exchange("http://inventory/reservations", HttpMethod.PATCH, new HttpEntity<>(request), Receipt.class).getBody();
  }
  Receipt cancel() {
    return webClient.method(HttpMethod.DELETE).uri("http://payments/charges").retrieve().bodyToMono(Receipt.class).block();
  }
  Receipt unknown(HttpMethod method) {
    return restTemplate.exchange("http://inventory/reservations", method, null, Receipt.class).getBody();
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(call.source, call.target_service, call.protocol, call.target_method, call.target_path) for call in result.static_service_calls] == [
        ("CheckoutService.reconcile", "inventory", "http", "PATCH", "/reservations"),
        ("CheckoutService.cancel", "payments", "http", "DELETE", "/charges"),
    ]


def test_native_literal_route_prefixes_are_composed(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''@RequestMapping("/api") class OrdersController {
  @PostMapping("/orders") Order create(Order order) { return order; }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "routes.go").write_text(
        '''package api
func Create() {}
func register() { orders := router.Group("/api/orders"); orders.POST("/create", Create) }
''',
        encoding="utf-8",
    )

    java = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")
    go = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert any(entry.name == "/api/orders" for entry in java.entrypoints)
    assert any(entry.name == "/api/orders/create" for entry in go.entrypoints)


def test_node_graphql_analyzer_exposes_mutation_and_rabbit_publish(tmp_path: Path):
    source = tmp_path / "resolvers.ts"
    source.write_text(
        '''export const resolvers = {
  Mutation: { createOrder: (_: unknown, input: CreateOrderInput, { service }) => { service.create(input); channel.publish("orders", "created", input, { headers: { schema_version: "1" } }); } }
};
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name) for entry in result.entrypoints] == [
        ("graphql", "MUTATION", "createOrder")
    ]
    assert any(edge.kind == "publishes" and edge.target == "channel.publish" for edge in result.edges)
    assert [(item.channel, item.routing_key, item.payload_type, item.message_version) for item in result.message_contracts] == [
        ("orders", "created", "CreateOrderInput", "1"),
    ]


def test_spring_analyzers_extract_literal_amqp_publications_with_declared_payloads(tmp_path: Path):
    (tmp_path / "OrderPublisher.java").write_text(
        '''class OrderPublisher {
  RabbitTemplate publisher;
  void publish(OrderCreated event) { publisher.convertAndSend("orders", "order.created", event, message -> { message.getMessageProperties().setHeader("schema_version", "1"); return message; }); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "PaymentPublisher.kt").write_text(
        '''class PaymentPublisher(private val publisher: AmqpTemplate) {
  fun publish(event: PaymentCreated) { publisher.convertAndSend("payments", "payment.created", event) }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(item.channel, item.routing_key, item.payload_type, item.message_version) for item in result.message_contracts] == [
        ("orders", "order.created", "OrderCreated", "1"),
        ("payments", "payment.created", "PaymentCreated", None),
    ]


def test_go_analyzer_extracts_literal_amqp_publications_with_declared_payloads(tmp_path: Path):
    source = tmp_path / "publisher.go"
    source.write_text(
        '''package orders
func publish(channel *amqp.Channel, event OrderCreated) error {
  return channel.Publish("orders", "order.created", false, false, amqp.Publishing{Body: event, Headers: amqp.Table{"schema_version": "1"}})
}
func publishWithContext(ctx context.Context, channel *amqp.Channel, event OrderCreated) error {
  return channel.PublishWithContext(ctx, "orders", "order.created", false, false, amqp.Publishing{Body: event, Headers: amqp.Table{"schema_version": "1"}})
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert [(item.channel, item.routing_key, item.payload_type, item.message_version) for item in result.message_contracts] == [
        ("orders", "order.created", "OrderCreated", "1"),
        ("orders", "order.created", "OrderCreated", "1"),
    ]


def test_node_analyzer_exposes_rabbit_consumer_and_its_bounded_handler_flow(tmp_path: Path):
    source = tmp_path / "consumer.ts"
    source.write_text(
        '''channel.assertExchange("orders", "topic");
channel.assertQueue("orders.created", { deadLetterRoutingKey: "orders.dlq", messageTtl: 5000 });
channel.bindQueue("orders.created", "orders", "order.created");
channel.consume("orders.created", async (message: OrderCreated) => {
  // idempotency key prevents duplicate handling
  const timeout = 500;
  await orderService.handle(message);
});
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name) for entry in result.entrypoints] == [
        ("message", "CONSUME", "orders.created")
    ]
    assert any(edge.source == "message.consume:orders.created" and edge.target == "orderService.handle" for edge in result.edges)
    assert result.contracts["message.consume:orders.created"]["payload"] == {
        "name": "message", "type": "OrderCreated", "required": True,
    }
    assert result.contracts["message.consume:orders.created"]["dead_letter_routing_key"] == "orders.dlq"
    assert result.contracts["message.consume:orders.created"]["retry_delay_ms"] == 5000
    assert result.contracts["message.consume:orders.created"]["idempotency"] == "detected"
    assert result.contracts["message.consume:orders.created"]["timeout"] == "detected"
    assert result.contracts["message.consume:orders.created"]["bindings"] == [
        {"exchange": "orders", "routing_key": "order.created"},
    ]


def test_rabbit_consumer_idempotency_is_scoped_to_its_handler(tmp_path: Path):
    (tmp_path / "consumer.ts").write_text(
        '''channel.consume("orders.created", async (message: OrderCreated) => {
  // idempotency key prevents duplicate handling
  const timeout = 500;
  await orderService.handle(message);
});
channel.consume("billing.created", async (message: BillingCreated) => {
  await billingService.handle(message);
});
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.contracts["message.consume:orders.created"]["idempotency"] == "detected"
    assert "idempotency" not in result.contracts["message.consume:billing.created"]
    assert result.contracts["message.consume:orders.created"]["timeout"] == "detected"
    assert "timeout" not in result.contracts["message.consume:billing.created"]


def test_node_analyzer_exposes_literal_express_route_and_named_handler_flow(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
const app = express();

function createOrder(req: Request, res: Response) {
  return orderService.create(req.body);
}

app.post("/orders", createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("http", "POST", "/orders", "orders.createOrder"),
    ]
    assert any(
        edge.source == "orders.createOrder" and edge.target == "orderService.create"
        for edge in result.edges
    )


def test_node_analyzer_includes_javascript_sources_for_node_ts_services(tmp_path: Path):
    (tmp_path / "orders.js").write_text(
        '''import express from "express";
const app = express();
function createOrder(req, res) { return orderService.create(req.body); }
app.post("/orders", createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("http", "POST", "/orders", "orders.createOrder"),
    ]
    assert any(
        edge.source == "orders.createOrder" and edge.target == "orderService.create"
        for edge in result.edges
    )


def test_node_analyzer_records_literal_express_route_middleware_per_entrypoint(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
const app = express();

function requireAuthentication(req: Request, res: Response, next: NextFunction) { next(); }
function validateOrder(req: Request, res: Response, next: NextFunction) { next(); }
function createOrder(req: Request, res: Response) { return orderService.create(req.body); }

app.post("/orders", requireAuthentication, validateOrder, createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints[0].contract == {
        "route_middlewares": [
            {"symbol": "requireAuthentication"},
            {"symbol": "validateOrder"},
        ],
    }


def test_node_analyzer_exposes_literal_express_head_and_options_routes(tmp_path: Path):
    (tmp_path / "health.ts").write_text(
        '''import express from "express";
const app = express();

function checkHealth(req: Request, res: Response) {
  return healthService.check();
}

app.head("/health", checkHealth);
app.options("/health", checkHealth);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("HEAD", "/health", "health.checkHealth"),
        ("OPTIONS", "/health", "health.checkHealth"),
    ]
    assert any(
        edge.source == "health.checkHealth" and edge.target == "healthService.check"
        for edge in result.edges
    )


def test_node_analyzer_exposes_literal_express_route_with_named_arrow_handler(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
const app = express();

const createOrder = async (req: Request, res: Response) => {
  return orderService.create(req.body);
};

app.post("/orders", createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("http", "POST", "/orders", "orders.createOrder"),
    ]
    assert any(
        edge.source == "orders.createOrder" and edge.target == "orderService.create"
        for edge in result.edges
    )


def test_node_analyzer_exposes_literal_express_route_with_inline_handler(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
const app = express();

app.post("/orders", async (req: Request, res: Response) => {
  return orderService.create(req.body);
});
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("http", "POST", "/orders", "orders.http.post:/orders"),
    ]
    assert any(
        edge.source == "orders.http.post:/orders" and edge.target == "orderService.create"
        for edge in result.edges
    )


def test_node_analyzer_exposes_literal_express_chained_route_and_handler_flow(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
const app = express();

function createOrder(req: Request, res: Response) {
  return orderService.create(req.body);
}

app.route("/orders").post(createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("http", "POST", "/orders", "orders.createOrder"),
    ]
    assert any(
        edge.source == "orders.createOrder" and edge.target == "orderService.create"
        for edge in result.edges
    )


def test_node_analyzer_resolves_a_literal_express_router_mount_prefix(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
const app = express();
const router = express.Router();

function createOrder(req: Request, res: Response) {
  return orderService.create(req.body);
}

app.use("/api", router);
router.post("/orders", createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("POST", "/api/orders", "orders.createOrder"),
    ]


def test_node_analyzer_skips_an_unmounted_express_router(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
const router = express.Router();

function createOrder(req: Request, res: Response) {
  return orderService.create(req.body);
}

router.post("/orders", createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints == []


def test_node_analyzer_skips_a_multiply_mounted_express_router(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
const app = express();
const router = express.Router();

function createOrder(req: Request, res: Response) {
  return orderService.create(req.body);
}

app.use("/api", router);
app.use("/internal", router);
router.post("/orders", createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints == []


def test_node_analyzer_exposes_literal_fastify_route_and_named_handler_flow(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import Fastify from "fastify";
const app = Fastify();

function createOrder(request: FastifyRequest, reply: FastifyReply) {
  return orderService.create(request.body);
}

app.post("/orders", createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("http", "POST", "/orders", "orders.createOrder"),
    ]
    assert any(
        edge.source == "orders.createOrder" and edge.target == "orderService.create"
        for edge in result.edges
    )


def test_node_analyzer_extracts_literal_express_and_fastify_error_mappings(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
import Fastify from "fastify";
const app = express();
const fastify = Fastify();

function createOrder(req: Request, res: Response) {
  return res.status(409).json({ code: "OUT_OF_STOCK" });
}

function findOrder(request: FastifyRequest, reply: FastifyReply) {
  return reply.code(404).send({ code: "ORDER_NOT_FOUND" });
}

function rejectOrder(req: Request, response: Response) {
  return response.sendStatus(400);
}

function notify(req: Request, client: PartnerClient) {
  return client.status(500).send();
}

app.post("/orders", createOrder);
app.delete("/orders/:id", rejectOrder);
fastify.get("/orders/:id", findOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(contract.source, contract.role, contract.error_kind, contract.protocol,
             contract.transport_code, contract.public_code, contract.exposes_internal_detail,
             contract.retryability)
            for contract in result.error_contracts] == [
        ("orders.createOrder", "maps", "conflict", "http", "409", None, False, "not_retryable"),
        ("orders.findOrder", "maps", "not_found", "http", "404", None, False, "not_retryable"),
        ("orders.rejectOrder", "maps", "validation", "http", "400", None, False, "not_retryable"),
    ]


def test_node_analyzer_marks_direct_error_details_in_http_reply_as_exposed(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import express from "express";
const app = express();

function failOrder(req: Request, res: Response) {
  return res.status(500).json({ message: error.message, stack: error.stack });
}

function rejectOrder(req: Request, res: Response) {
  return res.status(400).json({ code: "INVALID_ORDER" });
}

function redact(value: string) {
  return "REDACTED";
}

function redactFailure(req: Request, res: Response) {
  return res.status(500).json({ message: redact(error.message) });
}

function exposeCause(req: Request, res: Response) {
  return res.status(500).json({ cause: error.cause });
}

app.post("/orders", failOrder);
app.put("/orders/:id", rejectOrder);
app.post("/orders/redacted", redactFailure);
app.post("/orders/cause", exposeCause);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert {
        (contract.source, contract.transport_code): contract.exposes_internal_detail
        for contract in result.error_contracts
    } == {
        ("orders.failOrder", "500"): True,
        ("orders.rejectOrder", "400"): False,
        ("orders.redactFailure", "500"): False,
        ("orders.exposeCause", "500"): True,
    }


def test_node_analyzer_extracts_conventional_express_error_middleware(tmp_path: Path):
    (tmp_path / "errors.ts").write_text(
        '''import express from "express";
const app = express();

function handleError(error: Error, req: Request, res: Response, next: NextFunction) {
  return res.status(500).json({ message: error.message });
}

function orphanError(error: Error, req: Request, res: Response, next: NextFunction) {
  return res.status(500).json({ message: error.message });
}

app.use(handleError);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(contract.source, contract.role, contract.error_kind, contract.protocol,
             contract.transport_code, contract.exposes_internal_detail)
            for contract in result.error_contracts] == [
        ("errors.handleError", "maps", "unexpected", "http", "500", True),
    ]


def test_node_analyzer_extracts_conventional_fastify_error_handler(tmp_path: Path):
    (tmp_path / "errors.ts").write_text(
        '''import Fastify from "fastify";
const app = Fastify();

function handleError(error: Error, request: FastifyRequest, reply: FastifyReply) {
  return reply.code(500).send({ message: error.message });
}

function orphanError(error: Error, request: FastifyRequest, reply: FastifyReply) {
  return reply.code(500).send({ message: error.message });
}

app.setErrorHandler(handleError);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(contract.source, contract.role, contract.error_kind, contract.protocol,
             contract.transport_code, contract.exposes_internal_detail)
            for contract in result.error_contracts] == [
        ("errors.handleError", "maps", "unexpected", "http", "500", True),
    ]


def test_node_analyzer_extracts_literal_nest_exception_filter_mapping(tmp_path: Path):
    (tmp_path / "errors.ts").write_text(
        '''import { ArgumentsHost, Catch, ExceptionFilter } from "@nestjs/common";

@Catch()
class HttpExceptionFilter implements ExceptionFilter {
  catch(exception: Error, host: ArgumentsHost) {
    const response = host.switchToHttp().getResponse();
    return response.status(500).json({ message: exception.message });
  }
}

class InertFilter {
  catch(exception: Error, host: ArgumentsHost) {
    const response = host.switchToHttp().getResponse();
    return response.status(500).json({ message: exception.message });
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(contract.source, contract.role, contract.error_kind, contract.protocol,
             contract.transport_code, contract.exposes_internal_detail)
            for contract in result.error_contracts] == [
        ("HttpExceptionFilter.catch", "maps", "unexpected", "http", "500", True),
    ]


def test_node_analyzer_skips_fastify_like_route_without_a_local_factory(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''const app = makeTestServer();

function createOrder(request: Request, response: Response) {
  return orderService.create(request.body);
}

app.post("/orders", createOrder);
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints == []


def test_node_analyzer_exposes_literal_fastify_route_object_and_named_handler_flow(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import Fastify from "fastify";
const app = Fastify();

function createOrder(request: FastifyRequest, reply: FastifyReply) {
  return orderService.create(request.body);
}

app.route({ method: "POST", url: "/orders", handler: createOrder });
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("http", "POST", "/orders", "orders.createOrder"),
    ]
    assert any(
        edge.source == "orders.createOrder" and edge.target == "orderService.create"
        for edge in result.edges
    )


def test_node_analyzer_exposes_literal_fastify_route_object_with_multiple_methods(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import Fastify from "fastify";
const app = Fastify();

function readOrder(request: FastifyRequest, reply: FastifyReply) {
  return orderService.find(request.params.id);
}

app.route({ method: ["GET", "HEAD"], url: "/orders/:id", handler: readOrder });
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("GET", "/orders/:id", "orders.readOrder"),
        ("HEAD", "/orders/:id", "orders.readOrder"),
    ]
    assert any(
        edge.source == "orders.readOrder" and edge.target == "orderService.find"
        for edge in result.edges
    )


def test_node_analyzer_skips_fastify_route_object_with_dynamic_url(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''import Fastify from "fastify";
const app = Fastify();
const ordersUrl = "/orders";

function createOrder(request: FastifyRequest, reply: FastifyReply) {
  return orderService.create(request.body);
}

app.route({ method: "POST", url: ordersUrl, handler: createOrder });
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints == []


def test_node_analyzer_exposes_literal_nest_controller_route_and_method_flow(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text(
        '''import { Controller, Post } from "@nestjs/common";

@Controller("/orders")
export class OrdersController {
  @Post()
  create(input: CreateOrder) {
    return this.orderService.create(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("http", "POST", "/orders", "OrdersController.create"),
    ]
    assert any(
        edge.source == "OrdersController.create" and edge.target == "this.orderService.create"
        for edge in result.edges
    )


def test_node_analyzer_records_literal_nest_guards_per_entrypoint(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text(
        '''import { Controller, Post, UseGuards } from "@nestjs/common";

@Controller("/orders")
@UseGuards(AuthenticationGuard)
export class OrdersController {
  @Post()
  @UseGuards(OrdersPermissionGuard)
  create(input: CreateOrder) {
    return this.orderService.create(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints[0].contract == {
        "route_guards": [
            {"symbol": "AuthenticationGuard", "scope": "controller"},
            {"symbol": "OrdersPermissionGuard", "scope": "handler"},
        ],
    }


def test_node_analyzer_records_a_direct_nest_body_dto_per_entrypoint(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text(
        '''import { Body, Controller, Post, UseGuards } from "@nestjs/common";

@Controller("/orders")
@UseGuards(AuthenticationGuard)
export class OrdersController {
  @Post()
  create(@Body() input: CreateOrderDto) {
    return this.orderService.create(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints[0].contract == {
        "request": {"name": "input", "type": "CreateOrderDto", "required": True},
        "route_guards": [{"symbol": "AuthenticationGuard", "scope": "controller"}],
    }


def test_node_analyzer_records_literal_nest_route_parameters_per_entrypoint(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text(
        '''import { Controller, Get, Headers, Param, Query } from "@nestjs/common";

@Controller("/orders")
export class OrdersController {
  @Get(":id")
  find(
    @Param("id") id: string,
    @Query("includeArchived") includeArchived?: boolean,
    @Headers("x-request-id") requestId: RequestId,
  ) {
    return this.orderService.find(id, includeArchived, requestId);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints[0].contract == {
        "parameters": [
            {"kind": "path", "name": "id", "variable": "id", "type": "string", "required": True},
            {"kind": "query", "name": "includeArchived", "variable": "includeArchived", "type": "boolean", "required": False},
            {"kind": "header", "name": "x-request-id", "variable": "requestId", "type": "RequestId", "required": True},
        ],
    }


def test_node_analyzer_records_literal_nest_validation_pipes_per_entrypoint(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text(
        '''import { Controller, Post, UsePipes } from "@nestjs/common";

@Controller("/orders")
@UsePipes(ValidationPipe)
export class OrdersController {
  @Post()
  @UsePipes(CreateOrderPipe)
  create(input: CreateOrder) {
    return this.orderService.create(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints[0].contract == {
        "validation_pipes": [
            {"symbol": "ValidationPipe", "scope": "controller"},
            {"symbol": "CreateOrderPipe", "scope": "handler"},
        ],
    }


def test_node_analyzer_records_direct_nest_cache_and_rate_limit_decorators(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text(
        '''import { CacheKey, CacheTTL } from "@nestjs/cache-manager";
import { Controller, Post } from "@nestjs/common";
import { Throttle } from "@nestjs/throttler";

@Controller("/orders")
@CacheTTL(60000)
@Throttle()
export class OrdersController {
  @Post()
  @CacheKey("orders.create")
  @Throttle()
  create(input: CreateOrder) {
    return this.orderService.create(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints[0].contract == {
        "cache_decorators": [
            {"decorator": "CacheTTL", "scope": "controller"},
            {"decorator": "CacheKey", "scope": "handler"},
        ],
        "rate_limit_decorators": [
            {"decorator": "Throttle", "scope": "controller"},
            {"decorator": "Throttle", "scope": "handler"},
        ],
    }


def test_node_analyzer_skips_non_nest_cache_and_rate_limit_decorator_names(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text(
        '''import { Controller, Post } from "@nestjs/common";
import { CacheTTL, Throttle } from "./local-decorators";

@Controller("/orders")
@CacheTTL(60000)
export class OrdersController {
  @Post()
  @Throttle()
  create(input: CreateOrder) {
    return this.orderService.create(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints[0].contract is None


def test_node_analyzer_resolves_a_nest_constructor_injected_service_call(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text(
        '''import { Controller, Post } from "@nestjs/common";

export class OrdersService {
  create(input: CreateOrder) {
    return this.repository.save(input);
  }
}

@Controller("/orders")
export class OrdersController {
  constructor(private readonly ordersService: OrdersService) {}

  @Post()
  create(input: CreateOrder) {
    return this.ordersService.create(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(item.consumer, item.contract) for item in result.injections] == [
        ("OrdersController.ordersService", "OrdersService"),
    ]
    assert any(
        edge.source == "OrdersController.create" and edge.target == "OrdersService.create"
        for edge in result.edges
    )


def test_node_analyzer_resolves_constructor_dependencies_of_nest_injectables(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text(
        '''import { Controller, Injectable, Post } from "@nestjs/common";

@Injectable()
export class PaymentsService {
  charge(input: CreateOrder) { return input; }
}

@Injectable()
export class OrdersService {
  constructor(private readonly paymentsService: PaymentsService) {}

  create(input: CreateOrder) {
    return this.paymentsService.charge(input);
  }
}

@Controller("/orders")
export class OrdersController {
  constructor(private readonly ordersService: OrdersService) {}

  @Post()
  create(input: CreateOrder) {
    return this.ordersService.create(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(item.consumer, item.contract) for item in result.injections] == [
        ("OrdersService.paymentsService", "PaymentsService"),
        ("OrdersController.ordersService", "OrdersService"),
    ]
    assert any(
        edge.source == "OrdersService.create" and edge.target == "PaymentsService.charge"
        for edge in result.edges
    )


def test_go_analyzer_links_a_literal_amqp_queue_binding_to_its_consumer(tmp_path: Path):
    source = tmp_path / "consumer.go"
    source.write_text(
        '''package orders
func consume(channel *amqp.Channel) {
  channel.QueueDeclare("orders.created", true, false, false, false, amqp.Table{"x-dead-letter-routing-key": "orders.dlq", "x-message-ttl": 5000})
  channel.QueueBind("orders.created", "order.created", "orders", false, nil)
  channel.Consume("orders.created", "", false, false, false, false, func(message amqp.Delivery) {})
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert result.contracts["message.consume:orders.created"]["bindings"] == [
        {"exchange": "orders", "routing_key": "order.created"},
    ]
    assert result.contracts["message.consume:orders.created"]["dead_letter_routing_key"] == "orders.dlq"
    assert result.contracts["message.consume:orders.created"]["retry_delay_ms"] == 5000


def test_kotlin_analyzer_exposes_rabbit_listener_and_its_handler_flow(tmp_path: Path):
    source = tmp_path / "OrderListener.kt"
    source.write_text(
        '''class OrderListener {
  @RabbitListener(queues = ["orders.created"])
  fun consume(message: String) { orderService.handle(message) }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(entry.kind, entry.method, entry.name) for entry in result.entrypoints] == [
        ("message", "CONSUME", "orders.created")
    ]
    assert any(edge.source == "OrderListener.consume" and edge.target == "orderService.handle" for edge in result.edges)
    assert result.contracts["OrderListener.consume"] == {
        "transport": "rabbitmq", "direction": "consumes", "queue": "orders.created",
        "payload": {"name": "message", "type": "String", "required": True},
    }


def test_java_analyzer_links_a_literal_spring_queue_binding_to_its_consumer(tmp_path: Path):
    source = tmp_path / "OrderListener.java"
    source.write_text(
        '''class QueueConfig {
  Queue orderQueue() { return new Queue("orders.created"); }
  TopicExchange orderExchange() { return new TopicExchange("orders"); }
  Binding orderBinding() { return BindingBuilder.bind(orderQueue()).to(orderExchange()).with("order.created"); }
}
class OrderListener {
  @RabbitListener(queues = "orders.created")
  void consume(OrderCreated message) { orderService.handle(message); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert result.contracts["OrderListener.consume"]["bindings"] == [
        {"exchange": "orders", "routing_key": "order.created"},
    ]


def test_kotlin_analyzer_links_a_literal_spring_queue_binding_to_its_consumer(tmp_path: Path):
    source = tmp_path / "OrderListener.kt"
    source.write_text(
        '''class QueueConfig {
  fun orderQueue() = Queue("orders.created")
  fun orderExchange() = TopicExchange("orders")
  fun orderBinding() = BindingBuilder.bind(orderQueue()).to(orderExchange()).with("order.created")
}
class OrderListener {
  @RabbitListener(queues = ["orders.created"])
  fun consume(message: OrderCreated) { orderService.handle(message) }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert result.contracts["OrderListener.consume"]["bindings"] == [
        {"exchange": "orders", "routing_key": "order.created"},
    ]


def test_service_create_is_not_misclassified_as_direct_persistence(tmp_path: Path):
    source = tmp_path / "resolvers.ts"
    source.write_text(
        '''export const resolvers = {
  Mutation: { createOrder: (_, input, { service }) => service.create(input) }
};
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert any(edge.kind == "invokes" and edge.target == "service.create" for edge in result.edges)


def test_kotlin_analyzer_resolves_a_bounded_flow_across_injected_classes(tmp_path: Path):
    (tmp_path / "OrdersController.kt").write_text(
        '''class OrdersController(private val useCase: CreateOrderUseCase) {
  @PostMapping("/orders")
  fun create(request: OrderRequest) = useCase.execute(request)
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CreateOrderUseCase.kt").write_text(
        '''class CreateOrderUseCase(private val orderRepository: OrderRepository) {
  fun execute(request: OrderRequest) { orderRepository.save(request) }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("OrdersController.create", "CreateOrderUseCase.execute", "invokes"),
        ("CreateOrderUseCase.execute", "orderRepository.save", "writes"),
    }


def test_java_spring_analyzer_maps_controller_and_cross_file_use_case(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''@RestController
class OrdersController {
  private final CreateOrderUseCase useCase;
  OrdersController(CreateOrderUseCase useCase) { this.useCase = useCase; }
  @PostMapping("/orders")
  Order create(Order order) { return useCase.execute(order); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CreateOrderUseCase.java").write_text(
        '''class CreateOrderUseCase {
  private final OrderRepository repository;
  Order execute(Order order) { return repository.save(order); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(entry.method, entry.name, entry.symbol) for entry in result.entrypoints] == [
        ("POST", "/orders", "OrdersController.create")
    ]
    assert any(edge.target == "CreateOrderUseCase.execute" for edge in result.edges)
    assert any(edge.kind == "writes" and edge.target == "repository.save" for edge in result.edges)


def test_spring_analyzers_classify_only_locally_injected_repository_receivers(tmp_path: Path):
    (tmp_path / "Orders.java").write_text(
        '''class Orders {
  private final OrderRepository repository;
  Order find(String id) { return repository.findById(id); }
  Order save(Order order) { return repository.save(order); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Payments.kt").write_text(
        '''class Payments(private val repository: PaymentRepository) {
  fun find(id: String) = repository.findById(id)
  fun save(payment: Payment) = repository.save(payment)
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Unproven.java").write_text(
        '''class Unproven {
  Order save(Order order) { return repository.save(order); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("Orders.find", "repository.findById", "reads"),
        ("Orders.save", "repository.save", "writes"),
        ("Payments.find", "repository.findById", "reads"),
        ("Payments.save", "repository.save", "writes"),
        ("Unproven.save", "repository.save", "invokes"),
    }


def test_spring_data_derived_operations_require_a_local_repository_interface(tmp_path: Path):
    (tmp_path / "OrderRepository.java").write_text(
        '''interface OrderRepository extends JpaRepository<Order, String> {
  Order findByStatus(String status);
  long deleteByCustomerId(String customerId);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Orders.java").write_text(
        '''class Orders {
  private final OrderRepository repository;
  Order find(String status) { return repository.findByStatus(status); }
  long delete(String customerId) { return repository.deleteByCustomerId(customerId); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Unproven.java").write_text(
        '''class Unproven {
  private final UnknownRepository repository;
  Order find(String status) { return repository.findByStatus(status); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("Orders.find", "repository.findByStatus", "reads"),
        ("Orders.delete", "repository.deleteByCustomerId", "writes"),
        ("Unproven.find", "repository.findByStatus", "invokes"),
    }


def test_spring_data_query_operations_require_local_repository_and_modifying_evidence(tmp_path: Path):
    (tmp_path / "OrderRepository.java").write_text(
        '''interface OrderRepository extends JpaRepository<Order, String> {
  @Query("select o from Order o where o.status = :status")
  Order findActive(String status);
  @Query("update Order o set o.archived = true") @Modifying
  int archiveExpired();
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Orders.java").write_text(
        '''class Orders {
  private final OrderRepository repository;
  Order find(String status) { return repository.findActive(status); }
  int archive() { return repository.archiveExpired(); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Unproven.java").write_text(
        '''class Unproven {
  private final UnknownRepository repository;
  Order find(String status) { return repository.findActive(status); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("Orders.find", "repository.findActive", "reads"),
        ("Orders.archive", "repository.archiveExpired", "writes"),
        ("Unproven.find", "repository.findActive", "invokes"),
    }


def test_spring_analyzers_classify_only_explicit_jdbc_template_dependencies(tmp_path: Path):
    (tmp_path / "Orders.java").write_text(
        '''class Orders {
  private final JdbcTemplate jdbc;
  Order find(String id) { return jdbc.queryForObject("select id from orders", Order.class, id); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Payments.kt").write_text(
        '''class Payments(private val jdbc: NamedParameterJdbcTemplate) {
  fun archive() = jdbc.update("update payments set archived = true", emptyMap<String, Any>())
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Unproven.java").write_text(
        '''class Unproven {
  Order find(Client jdbc, String id) { return jdbc.queryForObject("select id from orders", Order.class, id); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("Orders.find", "jdbc.queryForObject", "reads"),
        ("Payments.archive", "jdbc.update", "writes"),
        ("Unproven.find", "jdbc.queryForObject", "invokes"),
    }


def test_spring_analyzers_classify_only_explicit_mongo_template_dependencies(tmp_path: Path):
    (tmp_path / "Orders.java").write_text(
        '''class Orders {
  private final MongoTemplate mongo;
  Order find(String id) { return mongo.findById(id, Order.class); }
  Order save(Order order) { return mongo.save(order); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Payments.kt").write_text(
        '''class Payments(private val mongo: ReactiveMongoTemplate) {
  fun remove(id: String) = mongo.remove(id)
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Unproven.java").write_text(
        '''class Unproven {
  Order find(Client mongo, String id) { return mongo.findById(id, Order.class); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("Orders.find", "mongo.findById", "reads"),
        ("Orders.save", "mongo.save", "writes"),
        ("Payments.remove", "mongo.remove", "writes"),
        ("Unproven.find", "mongo.findById", "invokes"),
    }


def test_spring_analyzers_classify_only_explicit_entity_manager_dependencies(tmp_path: Path):
    (tmp_path / "Orders.java").write_text(
        '''class Orders {
  private final EntityManager entityManager;
  Order find(String id) { return entityManager.find(Order.class, id); }
  void save(Order order) { entityManager.persist(order); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Payments.kt").write_text(
        '''class Payments(private val entityManager: EntityManager) {
  fun remove(payment: Payment) = entityManager.remove(payment)
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Unproven.java").write_text(
        '''class Unproven {
  Order find(Client entityManager, String id) { return entityManager.find(Order.class, id); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("Orders.find", "entityManager.find", "reads"),
        ("Orders.save", "entityManager.persist", "writes"),
        ("Payments.remove", "entityManager.remove", "writes"),
        ("Unproven.find", "entityManager.find", "invokes"),
    }


def test_graphql_schema_contract_is_linked_to_its_resolver_entrypoint(tmp_path: Path):
    (tmp_path / "resolvers.ts").write_text(
        '''export const resolvers = { Mutation: { createOrder: (_, input) => orderService.create(input) } };''',
        encoding="utf-8",
    )
    (tmp_path / "schema.graphql").write_text(
        '''type Mutation { createOrder(input: CreateOrderInput!): Order! }
input CreateOrderInput { sku: String! note: String }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.contracts["Mutation.createOrder"] == {
        "arguments": [{
            "name": "input", "type": "CreateOrderInput", "required": True,
            "fields": [
                {"name": "sku", "type": "String", "required": True},
                {"name": "note", "type": "String", "required": False},
            ],
        }],
        "returns": {"type": "Order", "required": True},
    }


def test_graphql_resolvers_extract_literal_graphql_error_codes(tmp_path: Path):
    (tmp_path / "resolvers.ts").write_text(
        '''import { GraphQLError as GqlError } from "graphql";
export const resolvers = {
  Mutation: {
    createOrder: (_: unknown, input: CreateOrderInput) => {
      if (!input.sku) throw new GqlError("invalid input", { extensions: { code: "BAD_USER_INPUT" } });
      if (!input.stock) throw new GqlError("out of stock", { extensions: { code: "OUT_OF_STOCK" } });
      if (input.expose) throw new GqlError(error.message, { extensions: { code: "INTERNAL_SERVER_ERROR" } });
      if (input.redacted) throw new GqlError(redact(error.message), { extensions: { code: "INTERNAL_SERVER_ERROR" } });
      if (input.dynamic) throw new GqlError("dynamic", { extensions: { code: input.dynamic } });
      return orderService.create(input);
    },
  },
};
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(contract.source, contract.role, contract.error_kind, contract.internal_type,
             contract.protocol, contract.transport_code, contract.public_code,
             contract.exposes_internal_detail, contract.retryability)
            for contract in result.error_contracts] == [
        ("Mutation.createOrder", "raises", "validation", "GraphQLError", "graphql", None,
         "BAD_USER_INPUT", False, "not_retryable"),
        ("Mutation.createOrder", "raises", "unknown", "GraphQLError", "graphql", None,
         "OUT_OF_STOCK", False, "not_retryable"),
        ("Mutation.createOrder", "raises", "unexpected", "GraphQLError", "graphql", None,
         "INTERNAL_SERVER_ERROR", True, "not_retryable"),
        ("Mutation.createOrder", "raises", "unexpected", "GraphQLError", "graphql", None,
         "INTERNAL_SERVER_ERROR", False, "not_retryable"),
    ]


def test_graphql_contract_includes_local_interface_and_union_return_options(tmp_path: Path):
    (tmp_path / "resolvers.ts").write_text(
        '''export const resolvers = { Query: { node: () => null, search: () => [] } };''', encoding="utf-8",
    )
    (tmp_path / "schema.graphql").write_text(
        '''type Query { node: Node! search: [SearchResult!]! }
interface Node { id: ID! }
type User implements Node { id: ID! }
type Order implements Node { id: ID! }
union SearchResult = User | Order
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.contracts["Query.node"]["returns"]["possible_types"] == ["Order", "User"]
    assert result.contracts["Query.search"]["returns"]["possible_types"] == ["Order", "User"]


def test_graphql_schema_extensions_are_composed_across_local_files(tmp_path: Path):
    (tmp_path / "resolvers.ts").write_text(
        '''export const resolvers = { Query: { health: () => "ok", order: () => null } };''', encoding="utf-8",
    )
    (tmp_path / "base.graphql").write_text('''type Query { health: String! }''', encoding="utf-8")
    (tmp_path / "orders.graphql").write_text(
        "extend type Query { order(id: ID!): Order! }\ntype Order { id: ID! }\n", encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.contracts["Query.order"] == {
        "arguments": [{"name": "id", "type": "ID", "required": True, "fields": []}],
        "returns": {"type": "Order", "required": True},
    }


def test_java_spring_http_contract_keeps_declared_payload_validation_and_auth(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''class OrdersController {
  @PostMapping("/orders") @PreAuthorize("hasRole('ORDER_WRITE')")
  Order create(@Valid @RequestBody CreateOrderRequest request) { return service.create(request); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CreateOrderRequest.java").write_text(
        '''class CreateOrderRequest {
  @NotBlank String sku;
  Integer quantity;
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert result.contracts["OrdersController.create"] == {
        "request": {"name": "request", "type": "CreateOrderRequest", "required": True, "fields": [
            {"name": "sku", "type": "String", "required": True, "validations": ["NotBlank"]},
            {"name": "quantity", "type": "Integer", "required": False, "validations": []},
        ]},
        "returns": {"type": "Order", "required": True},
        "validations": ["Valid"],
        "authorization": ["PreAuthorize"],
        "parameters": [],
    }


def test_rest_contract_extracts_literal_spring_and_go_parameter_bindings(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''class OrdersController {
  @GetMapping("/orders/{id}")
  Order get(@PathVariable("id") String id, @RequestParam("expand") String expand, @RequestHeader("X-Trace") String trace) { return null; }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "orders.go").write_text(
        '''package orders
func Get(w http.ResponseWriter, r *http.Request) { r.PathValue("id"); r.URL.Query().Get("expand"); r.Header.Get("X-Trace") }
func register() { router.GET("/orders/{id}", Get) }
''',
        encoding="utf-8",
    )

    java = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")
    go = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert [item["kind"] for item in java.contracts["OrdersController.get"]["parameters"]] == ["path", "query", "header"]
    assert [item["name"] for item in go.contracts["orders.Get"]["parameters"]] == ["id", "expand", "X-Trace"]


def test_rest_contract_extracts_literal_response_statuses(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''class OrdersController {
  @PostMapping("/orders") @ResponseStatus(HttpStatus.CREATED)
  Order create(Order order) { return order; }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "orders.go").write_text(
        '''package orders
func Create(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusCreated) }
func register() { router.POST("/orders", Create) }
''',
        encoding="utf-8",
    )

    java = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")
    go = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert java.contracts["OrdersController.create"]["response_statuses"] == [{"code": 201, "name": "CREATED"}]
    assert go.contracts["orders.Create"]["response_statuses"] == [{"code": 201, "name": "CREATED"}]


def test_go_analyzer_extracts_literal_http_error_mappings(tmp_path: Path):
    (tmp_path / "orders.go").write_text(
        '''package orders
import "net/http"

func Create(w http.ResponseWriter, r *http.Request) {
  http.Error(w, "out of stock", http.StatusConflict)
}

func Find(w http.ResponseWriter, r *http.Request) {
  w.WriteHeader(http.StatusNotFound)
}

func Reject(w http.ResponseWriter, r *http.Request) {
  w.WriteHeader(400)
}

func Expose(w http.ResponseWriter, r *http.Request, err error) {
  http.Error(w, err.Error(), http.StatusInternalServerError)
}

func Redacted(w http.ResponseWriter, r *http.Request, err error) {
  http.Error(w, redact(err.Error()), http.StatusInternalServerError)
}

func Dynamic(w http.ResponseWriter, r *http.Request, status int) {
  w.WriteHeader(status)
}

func External(w customWriter, r *http.Request) {
  w.WriteHeader(http.StatusInternalServerError)
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert [(contract.source, contract.role, contract.error_kind, contract.protocol,
             contract.transport_code, contract.public_code, contract.exposes_internal_detail,
             contract.retryability)
            for contract in result.error_contracts] == [
        ("orders.Create", "maps", "conflict", "http", "409", None, False, "not_retryable"),
        ("orders.Find", "maps", "not_found", "http", "404", None, False, "not_retryable"),
        ("orders.Reject", "maps", "validation", "http", "400", None, False, "not_retryable"),
        ("orders.Expose", "maps", "unexpected", "http", "500", None, True, "not_retryable"),
        ("orders.Redacted", "maps", "unexpected", "http", "500", None, False, "not_retryable"),
    ]


def test_native_flow_boundaries_are_extracted_from_declared_control_flow(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''class OrdersController {
  @PostMapping("/orders") @Transactional
  Order create(Order order) {
    if (order == null) { throw new IllegalArgumentException(); }
    retry(); return service.create(order);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {boundary.kind for boundary in result.boundaries if boundary.source == "OrdersController.create"} == {
        "branch", "retry", "error", "transaction",
    }


def test_spring_extracts_literal_resilience_limits_without_runtime_inference(tmp_path: Path):
    (tmp_path / "CheckoutClient.java").write_text(
        '''class CheckoutClient {
  private WebClient client;
  @Retryable(maxAttempts = 3)
  Receipt reserve() {
    return client.post().uri("http://inventory/reservations").retrieve()
        .bodyToMono(Receipt.class).timeout(Duration.ofSeconds(2)).retry(1);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(policy.source, policy.kind, policy.mechanism, policy.value, policy.unit)
            for policy in result.resilience_policies} == {
        ("CheckoutClient.reserve", "retry", "spring_annotation", 3, "attempts"),
        ("CheckoutClient.reserve", "timeout", "reactor", 2_000, "milliseconds"),
        ("CheckoutClient.reserve", "retry", "reactor", 1, "retries"),
    }


def test_spring_ignores_dynamic_or_unbounded_resilience_declarations(tmp_path: Path):
    (tmp_path / "CheckoutClient.kt").write_text(
        '''class CheckoutClient(private val client: WebClient) {
  @Retryable
  fun reserve() = client.post().uri("http://inventory/reservations").retrieve()
      .bodyToMono(Receipt::class.java).timeout(timeout).retryWhen(policy)
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert result.resilience_policies == []


def test_spring_extracts_explicit_timeout_fallbacks_as_handled_error_contracts(tmp_path: Path):
    (tmp_path / "CheckoutClient.java").write_text(
        '''class CheckoutClient {
  private WebClient client;
  Receipt reserve() {
    try {
      return client.post().uri("http://inventory/reservations").retrieve()
          .bodyToMono(Receipt.class).timeout(Duration.ofSeconds(2));
    } catch (TimeoutException error) { return Receipt.fallback(); }
  }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "PaymentClient.kt").write_text(
        '''class PaymentClient(private val client: WebClient) {
  fun charge() = client.post().uri("http://payments/charges").retrieve()
      .bodyToMono(Receipt::class.java).timeout(Duration.ofSeconds(2))
      .onErrorResume(TimeoutException::class.java) { Mono.empty() }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(contract.source, contract.role, contract.error_kind, contract.internal_type)
            for contract in result.error_contracts} == {
        ("CheckoutClient.reserve", "handles", "timeout", "TimeoutException"),
        ("PaymentClient.charge", "handles", "timeout", "TimeoutException"),
    }


def test_spring_marks_an_explicit_success_timeout_fallback_as_http_success(tmp_path: Path):
    (tmp_path / "CheckoutController.java").write_text(
        '''class CheckoutController {
  private WebClient client;
  @PostMapping("/checkout")
  ResponseEntity<Receipt> reserve() {
    try {
      return client.post().uri("http://inventory/reservations").retrieve()
          .bodyToMono(Receipt.class).timeout(Duration.ofSeconds(2)).block();
    } catch (TimeoutException error) { return ResponseEntity.ok(Receipt.cached()); }
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(contract.source, contract.role, contract.error_kind, contract.protocol, contract.transport_code)
            for contract in result.error_contracts] == [
        ("CheckoutController.reserve", "handles", "timeout", "http", "200"),
    ]


def test_spring_classifies_explicit_timeout_status_mappings(tmp_path: Path):
    (tmp_path / "TimeoutExceptionHandler.java").write_text(
        '''@ControllerAdvice
class TimeoutExceptionHandler {
  @ExceptionHandler(TimeoutException.class) @ResponseStatus(HttpStatus.INTERNAL_SERVER_ERROR)
  ApiError internal(TimeoutException error) { return new ApiError(); }
  @ExceptionHandler(SocketTimeoutException.class) @ResponseStatus(HttpStatus.SERVICE_UNAVAILABLE)
  ApiError unavailable(SocketTimeoutException error) { return new ApiError(); }
  @ExceptionHandler(ReadTimeoutException.class) @ResponseStatus(HttpStatus.GATEWAY_TIMEOUT)
  ApiError gateway(ReadTimeoutException error) { return new ApiError(); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(contract.source, contract.error_kind, contract.transport_code) for contract in result.error_contracts} == {
        ("TimeoutExceptionHandler.internal", "timeout", "500"),
        ("TimeoutExceptionHandler.unavailable", "timeout", "503"),
        ("TimeoutExceptionHandler.gateway", "timeout", "504"),
    }


def test_spring_exception_handler_emits_a_static_error_contract(tmp_path: Path):
    (tmp_path / "ApiExceptionHandler.java").write_text(
        '''@ControllerAdvice
class ApiExceptionHandler {
  @ExceptionHandler(InsufficientStockException.class)
  @ResponseStatus(HttpStatus.CONFLICT)
  ProblemDetail handleStock(InsufficientStockException error) {
    return ProblemDetail.forStatusAndDetail(HttpStatus.CONFLICT, error.getMessage());
  }
  @ExceptionHandler(InvalidOrderException.class)
  @ResponseStatus(HttpStatus.BAD_REQUEST)
  ProblemDetail handleInvalid(InvalidOrderException error) {
    return ProblemDetail.forStatusAndDetail(HttpStatus.BAD_REQUEST, redact(error.getMessage()));
  }
  @ExceptionHandler(PaymentException.class)
  @ResponseStatus(HttpStatus.INTERNAL_SERVER_ERROR)
  ResponseEntity<String> handlePayment(PaymentException error) {
    return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(error.getMessage());
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(contract.source, contract.role, contract.error_kind, contract.internal_type,
             contract.protocol, contract.transport_code, contract.public_code,
             contract.exposes_internal_detail, contract.retryability)
            for contract in result.error_contracts] == [
        ("ApiExceptionHandler.handleStock", "maps", "conflict", "InsufficientStockException",
         "http", "409", None, True, "not_retryable"),
        ("ApiExceptionHandler.handleInvalid", "maps", "validation", "InvalidOrderException",
         "http", "400", None, False, "not_retryable"),
        ("ApiExceptionHandler.handlePayment", "maps", "unexpected", "PaymentException",
         "http", "500", None, True, "not_retryable"),
    ]


def test_kotlin_spring_exception_handler_marks_direct_response_entity_detail(tmp_path: Path):
    (tmp_path / "ApiExceptionHandler.kt").write_text(
        '''@ControllerAdvice
class ApiExceptionHandler {
  @ExceptionHandler(PaymentException::class)
  @ResponseStatus(HttpStatus.INTERNAL_SERVER_ERROR)
  fun handlePayment(error: PaymentException): ResponseEntity<String> {
    return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(error.message)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(contract.source, contract.internal_type, contract.transport_code, contract.exposes_internal_detail)
            for contract in result.error_contracts] == [
        ("ApiExceptionHandler.handlePayment", "PaymentException", "500", True),
    ]


def test_spring_response_status_exception_emits_a_raised_error_contract(tmp_path: Path):
    (tmp_path / "StockReservation.java").write_text(
        '''class StockReservation {
  void reserve() { throw new ResponseStatusException(HttpStatus.CONFLICT); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(contract.source, contract.role, contract.error_kind, contract.internal_type,
             contract.protocol, contract.transport_code, contract.retryability)
            for contract in result.error_contracts] == [
        ("StockReservation.reserve", "raises", "conflict", "ResponseStatusException",
         "http", "409", "not_retryable"),
    ]


def test_static_persistence_facts_require_local_entity_evidence(tmp_path: Path):
    (tmp_path / "Order.java").write_text(
        '''@Entity @Table(name = "orders") class Order { String id; }''', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(fact.name, fact.kind, fact.owner) for fact in result.persistence_facts} == {("orders", "sql_table", "Order")}


def test_static_analysis_extracts_literal_sql_migration_operations_with_destructive_flags(tmp_path: Path):
    migration = tmp_path / "db" / "migration" / "V12__payment_method.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text(
        '''-- DROP TABLE ignored_comment;
CREATE TABLE payment_method (id UUID PRIMARY KEY);
ALTER TABLE payment_method ADD COLUMN provider VARCHAR(32);
CREATE INDEX payment_method_provider_idx ON payment_method (provider);
ALTER TABLE payment_method DROP COLUMN legacy_token;
DROP TABLE retired_payment_method;
INSERT INTO migration_audit (message) VALUES ('DROP TABLE only_in_a_message');
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [
        (fact.operation, fact.table_name, fact.column_name, fact.destructive,
         fact.evidence.file_path, fact.evidence.start_line)
        for fact in result.migration_facts
    ] == [
        ("create_table", "payment_method", None, False, "db/migration/V12__payment_method.sql", 2),
        ("add_column", "payment_method", "provider", False, "db/migration/V12__payment_method.sql", 3),
        ("create_index", "payment_method", None, False, "db/migration/V12__payment_method.sql", 4),
        ("drop_column", "payment_method", "legacy_token", True, "db/migration/V12__payment_method.sql", 5),
        ("drop_table", "retired_payment_method", None, True, "db/migration/V12__payment_method.sql", 6),
    ]


def test_static_analysis_ignores_sql_outside_a_recognized_migration_location(tmp_path: Path):
    (tmp_path / "schema.sql").write_text("DROP TABLE not_a_migration;", encoding="utf-8")

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert result.migration_facts == []


def test_static_analysis_extracts_literal_liquibase_xml_migration_operations(tmp_path: Path):
    changelog = tmp_path / "db" / "changelog" / "payment-method.xml"
    changelog.parent.mkdir(parents=True)
    changelog.write_text(
        '''<databaseChangeLog>
  <changeSet id="payment-method" author="orbitkb">
    <createTable tableName="payment_method"/>
    <addColumn tableName="payment_method"><column name="provider"/></addColumn>
    <createIndex tableName="payment_method" indexName="payment_method_provider_idx"/>
    <dropColumn tableName="payment_method" columnName="legacy_token"/>
    <dropTable tableName="retired_payment_method"/>
    <!-- <dropTable tableName="ignored_comment"/> -->
    <dropTable tableName="${runtime_table}"/>
  </changeSet>
</databaseChangeLog>
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [
        (fact.operation, fact.table_name, fact.column_name, fact.destructive,
         fact.evidence.file_path, fact.evidence.start_line)
        for fact in result.migration_facts
    ] == [
        ("create_table", "payment_method", None, False, "db/changelog/payment-method.xml", 3),
        ("add_column", "payment_method", "provider", False, "db/changelog/payment-method.xml", 4),
        ("create_index", "payment_method", None, False, "db/changelog/payment-method.xml", 5),
        ("drop_column", "payment_method", "legacy_token", True, "db/changelog/payment-method.xml", 6),
        ("drop_table", "retired_payment_method", None, True, "db/changelog/payment-method.xml", 7),
    ]


def test_static_analysis_extracts_prisma_sql_migration_operations(tmp_path: Path):
    migration = tmp_path / "prisma" / "migrations" / "20260924120000_payment_method" / "migration.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text(
        'CREATE TABLE "payment_method" ("id" TEXT PRIMARY KEY);\n', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [
        (fact.operation, fact.table_name, fact.column_name, fact.destructive,
         fact.evidence.file_path, fact.evidence.start_line)
        for fact in result.migration_facts
    ] == [
        ("create_table", "payment_method", None, False,
         "prisma/migrations/20260924120000_payment_method/migration.sql", 1),
    ]


def test_static_analysis_enriches_matching_endpoint_with_openapi_contract(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''@RestController
class OrdersController {
  @PostMapping("/orders")
  Order create(Order request) { return request; }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "openapi.yaml").write_text(
        '''openapi: 3.0.3
security:
  - bearerAuth: []
paths:
  /orders:
    post:
      operationId: createOrder
      requestBody:
        required: true
      responses:
        "201": {}
        "409": {}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert result.contracts["OrdersController.create"]["formal_contract"] == {
        "format": "openapi",
        "operation_id": "createOrder",
        "request_body_required": True,
        "response_statuses": ["201", "409"],
        "security": "required",
        "evidence": {"file": "openapi.yaml", "start_line": 7, "end_line": 7},
    }


def test_static_analysis_ignores_openapi_operation_without_an_exact_endpoint_match(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''@RestController
class OrdersController {
  @GetMapping("/orders/{id}")
  Order get(String id) { return new Order(); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "openapi.yaml").write_text(
        '''openapi: 3.0.3
paths:
  /orders/{orderId}:
    get:
      operationId: getOrder
      responses:
        "200": {}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert "formal_contract" not in result.contracts["OrdersController.get"]


def test_static_analysis_ignores_ambiguous_openapi_operations_for_one_endpoint(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''@RestController
class OrdersController {
  @GetMapping("/orders")
  Order get() { return new Order(); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "openapi.yaml").write_text(
        '''openapi: 3.0.3
paths:
  /orders:
    get:
      operationId: getOrder
      responses:
        "200": {}
''',
        encoding="utf-8",
    )
    (tmp_path / "swagger.yaml").write_text(
        '''swagger: "2.0"
paths:
  /orders:
    get:
      operationId: listOrders
      responses:
        "200": {}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert "formal_contract" not in result.contracts["OrdersController.get"]


def test_static_analysis_marks_referenced_openapi_request_body_as_unknown(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''@RestController
class OrdersController {
  @PostMapping("/orders")
  Order create(Order request) { return request; }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "openapi.yaml").write_text(
        '''openapi: 3.0.3
paths:
  /orders:
    post:
      operationId: createOrder
      requestBody:
        $ref: "#/components/requestBodies/CreateOrder"
      responses:
        "201": {}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert result.contracts["OrdersController.create"]["formal_contract"]["request_body_required"] is None


def test_static_analysis_extracts_literal_protobuf_service_rpcs(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

import "common/money.proto";
// rpc Ignored(FakeRequest) returns (FakeResponse);
service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
  rpc Watch(stream WatchRequest) returns (stream WatchResponse);
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert [
        (entry.kind, entry.method, entry.name, entry.symbol,
         entry.evidence.file_path, entry.evidence.start_line)
        for entry in result.entrypoints
    ] == [
        ("grpc", "RPC", "inventory.v1.Inventory.Reserve", "proto.inventory.v1.Inventory.Reserve",
         "inventory.proto", 7),
        ("grpc", "RPC", "inventory.v1.Inventory.Watch", "proto.inventory.v1.Inventory.Watch",
         "inventory.proto", 8),
    ]
    assert result.contracts["proto.inventory.v1.Inventory.Reserve"]["formal_contract"] == {
        "format": "protobuf",
        "package": "inventory.v1",
        "service": "Inventory",
        "rpc": "Reserve",
        "request": {"type": "ReserveRequest", "streaming": False},
        "response": {"type": "ReserveResponse", "streaming": False},
        "imports": ["common/money.proto"],
        "evidence": {"file": "inventory.proto", "start_line": 7, "end_line": 7},
    }


def test_static_analysis_links_a_literal_nest_grpc_handler_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "inventory.grpc-controller.ts").write_text(
        '''import { Controller } from "@nestjs/common";
import { GrpcMethod } from "@nestjs/microservices";

@Controller()
export class InventoryGrpcController {
  @GrpcMethod("Inventory", "Reserve")
  reserve(input: ReserveRequest) {
    return this.inventoryService.reserve(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert any(
        edge.source == "proto.inventory.v1.Inventory.Reserve"
        and edge.target == "InventoryGrpcController.reserve"
        and edge.kind == "invokes"
        for edge in result.edges
    )


def test_static_analysis_links_a_literal_nest_grpc_client_call_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "checkout.service.ts").write_text(
        '''import { Injectable } from "@nestjs/common";
import { ClientGrpc } from "@nestjs/microservices";

@Injectable()
export class CheckoutService {
  constructor(private readonly client: ClientGrpc) {}

  onModuleInit() {
    this.inventory = this.client.getService<InventoryService>("Inventory");
  }

  checkout(input: ReserveRequest) {
    return this.inventory.Reserve(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert any(
        edge.source == "CheckoutService.checkout"
        and edge.target == "proto.inventory.v1.Inventory.Reserve"
        and edge.kind == "invokes"
        for edge in result.edges
    )


def test_static_analysis_links_a_unique_camel_case_nest_grpc_client_method(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc ReserveStock(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "checkout.service.ts").write_text(
        '''import { Injectable } from "@nestjs/common";
import { ClientGrpc } from "@nestjs/microservices";

@Injectable()
export class CheckoutService {
  constructor(private readonly client: ClientGrpc) {}

  onModuleInit() {
    this.inventory = this.client.getService<InventoryService>("Inventory");
  }

  checkout(input: ReserveRequest) {
    return this.inventory.reserveStock(input);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert any(
        edge.source == "CheckoutService.checkout"
        and edge.target == "proto.inventory.v1.Inventory.ReserveStock"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_links_a_literal_java_grpc_service_handler_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "InventoryGrpcService.java").write_text(
        '''import io.grpc.stub.StreamObserver;
import net.devh.boot.grpc.server.service.GrpcService;

@GrpcService
public class InventoryGrpcService extends InventoryGrpc.InventoryImplBase {
  @Override
  public void reserve(ReserveRequest request, StreamObserver<ReserveResponse> observer) {
    inventoryService.reserve(request);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "proto.inventory.v1.Inventory.Reserve"
        and edge.target == "InventoryGrpcService.reserve"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_links_a_literal_kotlin_grpc_service_handler_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "InventoryGrpcService.kt").write_text(
        '''import net.devh.boot.grpc.server.service.GrpcService

@GrpcService
class InventoryGrpcService : InventoryGrpcKt.InventoryCoroutineImplBase() {
  override suspend fun reserve(request: ReserveRequest): ReserveResponse {
    return inventoryService.reserve(request)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "proto.inventory.v1.Inventory.Reserve"
        and edge.target == "InventoryGrpcService.reserve"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_links_a_kotlin_java_grpc_service_handler_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "InventoryGrpcService.kt").write_text(
        '''import net.devh.boot.grpc.server.service.GrpcService

@GrpcService
class InventoryGrpcService : InventoryGrpc.InventoryImplBase() {
  override fun reserve(request: ReserveRequest, observer: StreamObserver<ReserveResponse>) {
    inventoryService.reserve(request)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "proto.inventory.v1.Inventory.Reserve"
        and edge.target == "InventoryGrpcService.reserve"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_links_a_go_grpc_service_handler_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "inventory_server.go").write_text(
        '''package inventory

type InventoryServer struct {
  pb.UnimplementedInventoryServer
}

func (s *InventoryServer) Reserve(ctx context.Context, request *pb.ReserveRequest) (*pb.ReserveResponse, error) {
  return nil, nil
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert any(
        edge.source == "proto.inventory.v1.Inventory.Reserve"
        and edge.target == "InventoryServer.Reserve"
        and edge.confidence == "high"
        for edge in result.edges
    )


def test_static_analysis_links_a_go_grpc_service_handler_split_across_files(tmp_path: Path):
    """A struct embedding the generated server base and the method implementing it can live in
    different files of the same Go package -- `_go_grpc_handlers` must resolve this across two
    separate tree-sitter parses, not just within one file's tree. This is also the regression
    case for a tree-sitter `Tree` lifetime bug: `_go_grpc_handlers` used to defer `Node` access
    to a second pass over every file's methods, by which point its loop-local `tree` variable
    had been reassigned to the last file parsed, leaving earlier files' `Tree`s with no live
    reference while their `Node`s were still being read (the same defect fixed in
    `orbitkb/discovery/jvm_ast.py`, see `test_jvm_ast_tree_lifetime.py`).
    """
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "a_inventory_struct.go").write_text(
        '''package inventory

type InventoryServer struct {
  pb.UnimplementedInventoryServer
}
''',
        encoding="utf-8",
    )
    (tmp_path / "b_inventory_handler.go").write_text(
        '''package inventory

func (s *InventoryServer) Reserve(ctx context.Context, request *pb.ReserveRequest) (*pb.ReserveResponse, error) {
  return nil, nil
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert any(
        edge.source == "proto.inventory.v1.Inventory.Reserve"
        and edge.target == "InventoryServer.Reserve"
        and edge.confidence == "high"
        for edge in result.edges
    )


def test_static_analysis_ignores_a_go_named_generated_server_field(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "inventory_server.go").write_text(
        '''package inventory

type InventoryServer struct {
  generated pb.UnimplementedInventoryServer
}

func (s *InventoryServer) Reserve(ctx context.Context, request *pb.ReserveRequest) (*pb.ReserveResponse, error) {
  return nil, nil
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert not any(edge.target == "InventoryServer.Reserve" for edge in result.edges)


def test_static_analysis_links_a_go_grpc_client_call_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "checkout_service.go").write_text(
        '''package checkout

type CheckoutService struct {
  inventory pb.InventoryClient
}

func NewCheckoutService(conn *grpc.ClientConn) *CheckoutService {
  return &CheckoutService{inventory: pb.NewInventoryClient(conn)}
}

func (s *CheckoutService) Checkout(ctx context.Context, request *pb.ReserveRequest) error {
  _, err := s.inventory.Reserve(ctx, request)
  return err
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert any(
        edge.source == "CheckoutService.Checkout"
        and edge.target == "proto.inventory.v1.Inventory.Reserve"
        and edge.confidence == "high"
        for edge in result.edges
    )


def test_static_analysis_links_a_go_grpc_client_call_split_across_files(tmp_path: Path):
    """Twin of `..._go_grpc_service_handler_split_across_files` for `_go_grpc_client_bindings`:
    the struct/factory and the method that calls through it can live in different files, and
    that method's receiver was the other raw `Node` this function used to defer past its
    file-scanning loop's `tree` reassignment -- the same use-after-free class, on the client side.
    """
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "a_checkout_struct.go").write_text(
        '''package checkout

type CheckoutService struct {
  inventory pb.InventoryClient
}

func NewCheckoutService(conn *grpc.ClientConn) *CheckoutService {
  return &CheckoutService{inventory: pb.NewInventoryClient(conn)}
}
''',
        encoding="utf-8",
    )
    (tmp_path / "b_checkout_handler.go").write_text(
        '''package checkout

func (s *CheckoutService) Checkout(ctx context.Context, request *pb.ReserveRequest) error {
  _, err := s.inventory.Reserve(ctx, request)
  return err
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert any(
        edge.source == "CheckoutService.Checkout"
        and edge.target == "proto.inventory.v1.Inventory.Reserve"
        and edge.confidence == "high"
        for edge in result.edges
    )


def test_static_analysis_ignores_a_go_grpc_client_when_its_factory_does_not_match_the_field_type(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "checkout_service.go").write_text(
        '''package checkout

type CheckoutService struct {
  inventory pb.InventoryClient
}

func NewCheckoutService(conn *grpc.ClientConn) *CheckoutService {
  return &CheckoutService{inventory: pb.NewPaymentsClient(conn)}
}

func (s *CheckoutService) Checkout(ctx context.Context, request *pb.ReserveRequest) error {
  _, err := s.inventory.Reserve(ctx, request)
  return err
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert not any(edge.target == "proto.inventory.v1.Inventory.Reserve" for edge in result.edges)


def test_static_analysis_ignores_kotlin_grpc_handler_without_the_official_grpc_service_import(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "InventoryGrpcService.kt").write_text(
        '''import net.devh.boot.grpc.server.service.GrpcServiceFactory

@GrpcService
class InventoryGrpcService : InventoryGrpcKt.InventoryCoroutineImplBase() {
  override suspend fun reserve(request: ReserveRequest): ReserveResponse {
    return inventoryService.reserve(request)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert not any(edge.target == "InventoryGrpcService.reserve" for edge in result.edges)


def test_static_analysis_links_a_literal_java_grpc_stub_call_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.java").write_text(
        '''class CheckoutService {
  private final InventoryGrpc.InventoryBlockingStub inventoryStub;

  Receipt checkout(ReserveRequest request) {
    return inventoryStub.reserve(request);
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "CheckoutService.checkout"
        and edge.target == "proto.inventory.v1.Inventory.Reserve"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_links_a_literal_kotlin_grpc_stub_call_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.kt").write_text(
        '''class CheckoutService {
  private val inventoryStub: InventoryGrpcKt.InventoryCoroutineStub =
    InventoryGrpcKt.InventoryCoroutineStub(channel)

  suspend fun checkout(request: ReserveRequest): Receipt {
    return inventoryStub.reserve(request)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "CheckoutService.checkout"
        and edge.target == "proto.inventory.v1.Inventory.Reserve"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_links_a_kotlin_java_grpc_stub_call_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.kt").write_text(
        '''class CheckoutService {
  private val inventoryStub: InventoryGrpc.InventoryBlockingStub =
    InventoryGrpc.newBlockingStub(channel)

  fun checkout(request: ReserveRequest): Receipt {
    return inventoryStub.reserve(request)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "CheckoutService.checkout"
        and edge.target == "proto.inventory.v1.Inventory.Reserve"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_links_a_kotlin_constructor_grpc_stub_call_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.kt").write_text(
        '''class CheckoutService(
  private val inventoryStub: InventoryGrpcKt.InventoryCoroutineStub,
) {
  suspend fun checkout(request: ReserveRequest): Receipt {
    return inventoryStub.reserve(request)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "CheckoutService.checkout"
        and edge.target == "proto.inventory.v1.Inventory.Reserve"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_links_a_kotlin_inherited_grpc_stub_call_to_a_unique_proto_rpc(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.kt").write_text(
        '''open class BaseClient(
  protected val inventoryStub: InventoryGrpcKt.InventoryCoroutineStub,
)

class CheckoutService(stub: InventoryGrpcKt.InventoryCoroutineStub) : BaseClient(stub) {
  suspend fun checkout(request: ReserveRequest): Receipt {
    return inventoryStub.reserve(request)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "CheckoutService.checkout"
        and edge.target == "proto.inventory.v1.Inventory.Reserve"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_prefers_a_kotlin_subclass_grpc_stub_over_its_inherited_property(tmp_path: Path):
    (tmp_path / "inventory.proto").write_text(
        '''syntax = "proto3";
package inventory.v1;

service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CheckoutService.kt").write_text(
        '''open class BaseClient(
  protected open val inventoryStub: InventoryGrpcKt.InventoryCoroutineStub,
)

class CheckoutService(
  override val inventoryStub: InventoryGrpcKt.InventoryCoroutineStub,
) : BaseClient(inventoryStub) {
  suspend fun checkout(request: ReserveRequest): Receipt {
    return inventoryStub.reserve(request)
  }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "CheckoutService.checkout"
        and edge.target == "proto.inventory.v1.Inventory.Reserve"
        and edge.confidence == "medium"
        for edge in result.edges
    )


def test_static_analysis_ignores_ambiguous_protobuf_rpc_declarations(tmp_path: Path):
    for name in ("inventory.proto", "inventory-duplicate.proto"):
        (tmp_path / name).write_text(
            '''syntax = "proto3";
package inventory.v1;
service Inventory {
  rpc Reserve(ReserveRequest) returns (ReserveResponse);
}
''',
            encoding="utf-8",
        )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert result.entrypoints == []
    assert result.contracts == {}


def test_static_analysis_extracts_literal_node_environment_bindings_without_values(tmp_path: Path):
    (tmp_path / "publisher.ts").write_text(
        '''export function publish() {
  const topic = process.env.ORDERS_TOPIC;
  const apiKey = process.env["STRIPE_SECRET_KEY"];
  const prose = "process.env.IGNORED";
  // process.env.COMMENT_ONLY
  return { topic, apiKey, prose };
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [
        (binding.source, binding.key, binding.kind, binding.sensitive,
         binding.evidence.file_path, binding.evidence.start_line)
        for binding in result.configuration_bindings
    ] == [
        ("publisher.publish", "ORDERS_TOPIC", "environment", False, "publisher.ts", 2),
        ("publisher.publish", "STRIPE_SECRET_KEY", "environment", True, "publisher.ts", 3),
    ]


def test_node_analyzer_extracts_literal_launchdarkly_feature_flag_reads(tmp_path: Path):
    (tmp_path / "checkout.ts").write_text(
        '''import { initialize as initializeFlags } from "launchdarkly-node-server-sdk";
const flags = initializeFlags(process.env.LAUNCHDARKLY_SDK_KEY);

export function checkout(context: Context) {
  const enabled = flags.variation("checkout.new-payment-flow", context, false);
  const style = flags.stringVariation("checkout.button-style", context, "classic");
  return { enabled, style };
}

export function ignored(client: FlagClient, context: Context) {
  return client.variation("not-a-proven-flag-client", context, false);
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(flag.source, flag.key, flag.provider, flag.evidence.file_path, flag.evidence.start_line)
            for flag in result.feature_flags] == [
        ("checkout.checkout", "checkout.new-payment-flow", "launchdarkly", "checkout.ts", 5),
        ("checkout.checkout", "checkout.button-style", "launchdarkly", "checkout.ts", 6),
    ]


def test_static_analysis_extracts_literal_jvm_and_go_environment_bindings(tmp_path: Path):
    (tmp_path / "Config.java").write_text(
        '''class Config {
  String paymentUrl() { return System.getenv("PAYMENTS_URL"); }
  String timeout() { return System.getProperty("payments.timeout-ms"); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Config.kt").write_text(
        '''class KotlinConfig {
  fun topic() = System.getenv("ORDERS_TOPIC")
}
''',
        encoding="utf-8",
    )
    (tmp_path / "config.go").write_text(
        '''package config
import "os"
func Credentials() (string, bool) { return os.LookupEnv("PARTNER_API_TOKEN") }
''',
        encoding="utf-8",
    )

    jvm = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")
    go = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert {(binding.source, binding.key, binding.kind, binding.sensitive) for binding in jvm.configuration_bindings} == {
        ("Config.paymentUrl", "PAYMENTS_URL", "environment", False),
        ("Config.timeout", "payments.timeout-ms", "property", False),
        ("KotlinConfig.topic", "ORDERS_TOPIC", "environment", False),
    }
    assert [(binding.source, binding.key, binding.kind, binding.sensitive) for binding in go.configuration_bindings] == [
        ("config.Credentials", "PARTNER_API_TOKEN", "environment", True),
    ]


def test_static_analysis_extracts_literal_spring_value_property_bindings(tmp_path: Path):
    (tmp_path / "PaymentClient.java").write_text(
        '''class PaymentClient {
  @Value("${payments.base-url}")
  private String baseUrl;
  @Value("${payments.retries:3}")
  private int retries;
  String pay() { return baseUrl; }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "OrdersConfig.kt").write_text(
        '''class OrdersConfig(
  @Value("${orders.topic}") private val topic: String,
  @Value("${PARTNER_API_TOKEN}") private val token: String,
) {
  fun publish() = topic
}
''',
        encoding="utf-8",
    )
    (tmp_path / "IgnoredConfig.java").write_text(
        '''class IgnoredConfig {
  @Value("#{environment['PAYMENTS_URL']}") String spel;
  @Value("prefix-${payments.host}") String composed;
  @Value("${first}-${second}") String multiple;
  String read() { return spel; }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "BillingConfig.java").write_text(
        '''class BillingConfig {
  BillingConfig(@Value("${billing.timeout-ms}") int timeout) {}
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(binding.source, binding.key, binding.kind, binding.sensitive) for binding in result.configuration_bindings} == {
        ("PaymentClient.baseUrl", "payments.base-url", "property", False),
        ("PaymentClient.retries", "payments.retries", "property", False),
        ("OrdersConfig.topic", "orders.topic", "property", False),
        ("OrdersConfig.token", "PARTNER_API_TOKEN", "property", True),
        ("BillingConfig.timeout", "billing.timeout-ms", "property", False),
    }


def test_static_analysis_extracts_literal_spring_configuration_properties_bindings(tmp_path: Path):
    (tmp_path / "PaymentProperties.java").write_text(
        '''@ConfigurationProperties(prefix = "payments")
class PaymentProperties {
  private String baseUrl;
  private int retryCount;
  private String apiToken;
}
''',
        encoding="utf-8",
    )
    (tmp_path / "OrderProperties.kt").write_text(
        '''@ConfigurationProperties("orders")
class OrderProperties(
  val topicName: String,
  val maxRetries: Int,
)
''',
        encoding="utf-8",
    )
    (tmp_path / "DynamicProperties.java").write_text(
        '''@ConfigurationProperties(prefix = PROPERTY_PREFIX)
class DynamicProperties {
  private String ignored;
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert {(binding.source, binding.key, binding.kind, binding.sensitive) for binding in result.configuration_bindings} == {
        ("PaymentProperties.baseUrl", "payments.base-url", "property", False),
        ("PaymentProperties.retryCount", "payments.retry-count", "property", False),
        ("PaymentProperties.apiToken", "payments.api-token", "property", True),
        ("OrderProperties.topicName", "orders.topic-name", "property", False),
        ("OrderProperties.maxRetries", "orders.max-retries", "property", False),
    }


def test_static_analysis_ignores_go_environment_like_calls_without_os_import(tmp_path: Path):
    (tmp_path / "config.go").write_text(
        '''package config
func Lookup(os Config) string { return os.Getenv("NOT_AN_ENVIRONMENT_KEY") }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert result.configuration_bindings == []


def test_node_analyzer_extracts_literal_mongoose_collection_ownership(tmp_path: Path):
    (tmp_path / "order-model.ts").write_text(
        '''const Order = mongoose.model("Order", orderSchema, "orders");''', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(fact.name, fact.kind, fact.owner) for fact in result.persistence_facts] == [
        ("orders", "document", "Order"),
    ]


def test_node_analyzer_classifies_explicit_mongoose_model_operations(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''const Order = mongoose.model("Order", orderSchema, "orders");
function findOrder(id: string) { return Order.findById(id); }
function createOrder(input: CreateOrderInput) { return Order.create(input); }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("orders.findOrder", "Order.findById", "reads"),
        ("orders.createOrder", "Order.create", "writes"),
    }


def test_spring_analyzers_extract_literal_document_collection_ownership(tmp_path: Path):
    (tmp_path / "Order.java").write_text(
        '''@Document(collection = "orders") class Order {}''', encoding="utf-8",
    )
    (tmp_path / "Payment.kt").write_text(
        '''@Document("payments") data class Payment(val id: String)''', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(fact.name, fact.kind, fact.owner) for fact in result.persistence_facts] == [
        ("orders", "document", "Order"),
        ("payments", "document", "Payment"),
    ]


def test_node_analyzer_extracts_literal_prisma_model_ownership(tmp_path: Path):
    (tmp_path / "schema.prisma").write_text(
        '''datasource db {
  provider = "postgresql"
}
model Order {
  id String @id
  @@map("orders")
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(fact.name, fact.kind, fact.owner) for fact in result.persistence_facts] == [
        ("orders", "sql_table", "Order"),
    ]


def test_node_analyzer_classifies_explicit_prisma_client_operations(tmp_path: Path):
    (tmp_path / "orders.ts").write_text(
        '''const prisma = new PrismaClient();
function findOrder(id: string) { return prisma.order.findUnique({ where: { id } }); }
function upsertOrder(input: CreateOrderInput) { return prisma.order.upsert({ create: input }); }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert {(edge.source, edge.target, edge.kind) for edge in result.edges} >= {
        ("orders.findOrder", "prisma.order.findUnique", "reads"),
        ("orders.upsertOrder", "prisma.order.upsert", "writes"),
    }


def test_go_http_contract_keeps_the_json_decoded_payload_type(tmp_path: Path):
    (tmp_path / "orders.go").write_text(
        '''package orders
func Create(w http.ResponseWriter, r *http.Request) {
  var request CreateOrderRequest
  json.NewDecoder(r.Body).Decode(&request)
}
func register() { router.POST("/orders", Create) }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert result.contracts["orders.Create"] == {
        "request": {"name": "request", "type": "CreateOrderRequest", "required": True},
        "returns": None,
        "validations": [],
        "authorization": [],
        "parameters": [],
    }


def test_typed_symbol_index_resolves_an_injected_leaf_method_without_outgoing_calls(tmp_path: Path):
    (tmp_path / "OrdersController.kt").write_text(
        '''class OrdersController(private val useCase: CreateOrderUseCase) {
  @PostMapping("/orders")
  fun create(request: OrderRequest) = useCase.execute(request)
}
''',
        encoding="utf-8",
    )
    (tmp_path / "CreateOrderUseCase.kt").write_text(
        '''class CreateOrderUseCase { fun execute(request: OrderRequest) = Unit }
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(symbol.name == "CreateOrderUseCase.execute" for symbol in result.symbols)
    assert any(edge.target == "CreateOrderUseCase.execute" for edge in result.edges)


def test_java_interface_injection_resolves_a_unique_implementation_method(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''class OrdersController {
  private final OrderService service;
  @PostMapping("/orders")
  Order create(Order order) { return service.create(order); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "DefaultOrderService.java").write_text(
        '''class DefaultOrderService implements OrderService {
  Order create(Order order) { return order; }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(edge.target == "DefaultOrderService.create" for edge in result.edges)


def test_typed_symbol_index_keeps_an_ambiguous_call_unresolved(tmp_path: Path):
    (tmp_path / "Handler.java").write_text(
        '''class Handler {
  @PostMapping("/orders")
  Order create(Order order) { return worker.execute(order); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "FirstWorker.java").write_text(
        "class FirstWorker { Order execute(Order order) { return order; } }\n", encoding="utf-8",
    )
    (tmp_path / "SecondWorker.java").write_text(
        "class SecondWorker { Order execute(Order order) { return order; } }\n", encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(edge.target == "worker.execute" for edge in result.edges)


def test_node_import_alias_resolves_the_declared_module_among_homonymous_symbols(tmp_path: Path):
    (tmp_path / "resolvers.ts").write_text(
        '''import { createOrder as createExternalOrder } from "./orders-service";
export const resolvers = { Mutation: { createOrder: (_, input) => createExternalOrder(input) } };
''',
        encoding="utf-8",
    )
    (tmp_path / "orders-service.ts").write_text(
        "export function createOrder(input: unknown) { return input; }\n", encoding="utf-8",
    )
    (tmp_path / "admin-service.ts").write_text(
        "export function createOrder(input: unknown) { return input; }\n", encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert any(edge.source == "Mutation.createOrder" and edge.target == "orders-service.createOrder" for edge in result.edges)


def test_go_import_alias_resolves_a_declared_package_function(tmp_path: Path):
    (tmp_path / "handler.go").write_text(
        '''package api
import ordercommands "example.com/shop/orders"
func Create() { ordercommands.Create() }
func register() { router.POST("/orders", Create) }
''',
        encoding="utf-8",
    )
    (tmp_path / "orders.go").write_text(
        '''package orders
func Create() {}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert any(edge.source == "api.Create" and edge.target == "orders.Create" for edge in result.edges)


def test_java_qualifier_resolves_the_selected_interface_implementation(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''class OrdersController {
  @Qualifier("partnerAuthorizer") private OrderAuthorizer authorizer;
  @PostMapping("/orders")
  Order create(Order order) { return authorizer.authorize(order); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Authorizers.java").write_text(
        '''interface OrderAuthorizer { Order authorize(Order order); }
@Qualifier("localAuthorizer") class LocalAuthorizer implements OrderAuthorizer {
  public Order authorize(Order order) { return order; }
}
@Qualifier("partnerAuthorizer") class PartnerAuthorizer implements OrderAuthorizer {
  public Order authorize(Order order) { return order; }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "OrdersController.create" and edge.target == "PartnerAuthorizer.authorize"
        for edge in result.edges
    )


def test_java_primary_resolves_an_unqualified_interface_implementation(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''class OrdersController {
  private OrderAuthorizer authorizer;
  @PostMapping("/orders")
  Order create(Order order) { return authorizer.authorize(order); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "Authorizers.java").write_text(
        '''interface OrderAuthorizer { Order authorize(Order order); }
class LocalAuthorizer implements OrderAuthorizer {
  public Order authorize(Order order) { return order; }
}
@Primary class DefaultAuthorizer implements OrderAuthorizer {
  public Order authorize(Order order) { return order; }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert any(
        edge.source == "OrdersController.create" and edge.target == "DefaultAuthorizer.authorize"
        for edge in result.edges
    )


def test_spring_scheduled_job_exposes_only_proven_schedule_policy(tmp_path: Path):
    (tmp_path / "ReconciliationJob.java").write_text(
        '''class ReconciliationJob {
  @Scheduled(cron = "0 */5 * * * *")
  void reconcile() { retry(); ledger.sync(); }
}
''', encoding="utf-8",
    )
    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    job = result.entrypoints[0]
    assert (job.kind, job.method, job.name, job.symbol) == ("job", "SCHEDULED", "reconcile", "ReconciliationJob.reconcile")
    assert result.contracts[job.symbol] == {"schedule": "0 */5 * * * *", "concurrency": "unknown", "idempotency": "unknown"}
