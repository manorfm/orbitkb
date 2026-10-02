"""WP14: Kafka gets the same deterministic producer/consumer coverage
RabbitMQ already has, following the exact patterns the project's own
discovery-layer regexes already confirmed for each language/SDK
(orbitkb/discovery/{go,node_ts,python,jvm}_stack.py) -- `KafkaTemplate.send`
+ `@KafkaListener` (JVM), `*kafka.Writer`/`*kafka.Reader` (Go),
`producer.send({topic,messages})` + `consumer.subscribe`/`.run` (Node).
Python is intentionally out of scope here: `producer.send`/`.produce` already
gets a generic `publishes`-kind FlowEdge from the pre-existing `_call_kind`
substring classifier (its target text contains "produce"), consistent with
the plan's "best-effort" framing -- no MessageContract infrastructure exists
for Python at all, and building one is a separate, larger change.
"""
from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine


def test_spring_analyzers_extract_kafka_publications_with_declared_payloads(tmp_path: Path):
    (tmp_path / "OrderPublisher.java").write_text(
        '''class OrderPublisher {
  KafkaTemplate<String, Object> publisher;
  void publish(OrderCreated event) { publisher.send("orders", event); }
}
''',
        encoding="utf-8",
    )
    (tmp_path / "PaymentPublisher.kt").write_text(
        '''class PaymentPublisher(private val publisher: KafkaTemplate<String, Any>) {
  fun publish(key: String, event: PaymentCreated) { publisher.send("payments", key, event) }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(item.channel, item.payload_type) for item in result.message_contracts] == [
        ("orders", "OrderCreated"),
        ("payments", "PaymentCreated"),
    ]


def test_java_analyzer_exposes_kafka_listener_and_its_handler_flow(tmp_path: Path):
    (tmp_path / "OrderConsumer.java").write_text(
        '''class OrderConsumer {
  @KafkaListener(topics = "orders.created")
  void handle(OrderCreated event) { orderService.process(event); }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(entry.kind, entry.method, entry.name) for entry in result.entrypoints] == [
        ("message", "CONSUME", "orders.created"),
    ]
    symbol = "OrderConsumer.handle"
    assert result.contracts[symbol]["transport"] == "kafka"
    assert result.contracts[symbol]["payload"] == {"name": "event", "type": "OrderCreated", "required": True}
    assert any(edge.source == symbol and edge.target == "orderService.process" for edge in result.edges)


def test_kotlin_analyzer_exposes_kafka_listener_with_array_topics_syntax(tmp_path: Path):
    (tmp_path / "OrderConsumer.kt").write_text(
        '''class OrderConsumer {
  @KafkaListener(topics = ["orders.created"])
  fun handle(event: OrderCreated) { orderService.process(event) }
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "jvm-spring")

    assert [(entry.kind, entry.method, entry.name) for entry in result.entrypoints] == [
        ("message", "CONSUME", "orders.created"),
    ]


def test_go_analyzer_extracts_a_literal_kafka_publication_with_declared_payload(tmp_path: Path):
    (tmp_path / "publisher.go").write_text(
        '''package orders
func Publish(writer *kafka.Writer, event OrderCreated) error {
  return writer.WriteMessages(ctx, kafka.Message{Topic: "orders.created", Value: []byte(event)})
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert [(item.channel, item.payload_type) for item in result.message_contracts] == [
        ("orders.created", "OrderCreated"),
    ]


def test_go_analyzer_exposes_a_kafka_reader_loop_as_its_own_entrypoint(tmp_path: Path):
    (tmp_path / "consumer.go").write_text(
        '''package orders
func Consume() {
  reader := kafka.NewReader(kafka.ReaderConfig{Topic: "orders.created", GroupID: "orders-service"})
  msg, err := reader.ReadMessage(ctx)
  orderService.Process(msg)
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert [(entry.kind, entry.method, entry.name) for entry in result.entrypoints] == [
        ("message", "CONSUME", "orders.created"),
    ]
    assert result.entrypoints[0].symbol == "orders.Consume"
    assert result.contracts["orders.Consume"]["transport"] == "kafka"


def test_go_analyzer_ignores_a_kafka_reader_never_read_from(tmp_path: Path):
    """A reader declared with a literal Topic but never `.ReadMessage`'d in
    the same function isn't a real consumer entrypoint -- never a guess."""
    (tmp_path / "setup.go").write_text(
        '''package orders
func BuildReader() *kafka.Reader {
  return kafka.NewReader(kafka.ReaderConfig{Topic: "orders.created", GroupID: "orders-service"})
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "go")

    assert result.entrypoints == []


def test_node_analyzer_extracts_a_kafka_publication_by_its_object_literal_shape(tmp_path: Path):
    (tmp_path / "publisher.ts").write_text(
        '''export async function publish(event: OrderCreated) {
  await producer.send({ topic: "orders.created", messages: [{ key: event.id, value: JSON.stringify(event) }] });
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(item.channel, item.routing_key) for item in result.message_contracts] == [("orders.created", None)]


def test_node_analyzer_ignores_an_unrelated_send_call(tmp_path: Path):
    """`.send` alone is far too generic a method name (Express's
    `res.send()`, among others) -- only the literal `topic`+`messages`
    object-literal shape is proof, not the method name."""
    (tmp_path / "handler.ts").write_text(
        '''export function handle(res) {
  res.send({ status: "ok" });
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.message_contracts == []


def test_node_analyzer_exposes_kafka_consumer_and_its_bounded_handler_flow(tmp_path: Path):
    (tmp_path / "consumer.ts").write_text(
        '''import { Kafka } from "kafkajs";
const kafka = new Kafka({ brokers: ["localhost:9092"] });
const consumer = kafka.consumer({ groupId: "orders" });
async function run() {
  await consumer.subscribe({ topic: "orders.created", fromBeginning: true });
  await consumer.run({
    eachMessage: async ({ message }) => {
      await orderService.handle(message);
    },
  });
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(entry.kind, entry.method, entry.name) for entry in result.entrypoints] == [
        ("message", "CONSUME", "orders.created"),
    ]
    symbol = "message.consume:orders.created"
    assert result.contracts[symbol]["transport"] == "kafka"
    assert any(edge.source == symbol and edge.target == "orderService.handle" for edge in result.edges)
    assert [(item.direction, item.channel, item.evidence.start_line) for item in result.message_contracts] == [
        ("consumes", "orders.created", 5),
    ]


def test_node_analyzer_does_not_treat_unrelated_subscribe_as_kafka(tmp_path: Path):
    (tmp_path / "consumer.ts").write_text(
        '''const consumer = newsletterClient;
await consumer.subscribe({ topic: "orders.created" });
await consumer.run({ eachMessage: async ({ message }) => { await handle(message); } });
''', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.message_contracts == []
    assert not any(entry.kind == "message" for entry in result.entrypoints)


def test_node_analyzer_does_not_pair_different_kafka_consumers(tmp_path: Path):
    (tmp_path / "consumer.ts").write_text(
        '''import { Kafka } from "kafkajs";
const kafka = new Kafka({ brokers: ["localhost:9092"] });
const first = kafka.consumer({ groupId: "first" });
const second = kafka.consumer({ groupId: "second" });
await first.subscribe({ topic: "orders.created" });
await second.run({ eachMessage: async ({ message }) => { await handle(message); } });
''', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.message_contracts == []
    assert not any(entry.kind == "message" for entry in result.entrypoints)


def test_node_analyzer_does_not_pair_subscription_and_handler_in_separate_functions(tmp_path: Path):
    (tmp_path / "consumer.ts").write_text(
        '''import { Kafka } from "kafkajs";
const kafka = new Kafka({ brokers: ["localhost:9092"] });
const consumer = kafka.consumer({ groupId: "orders" });
async function subscribe() { await consumer.subscribe({ topic: "orders.created" }); }
async function start() {
  await consumer.run({ eachMessage: async ({ message }) => { await handle(message); } });
}
''', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.message_contracts == []
    assert not any(entry.kind == "message" for entry in result.entrypoints)


def test_python_kafka_producer_call_already_gets_a_generic_publishes_edge(tmp_path: Path):
    """No new Python code was written for this WP: `producer.send(...)`'s
    target text already contains "produce" as a substring of "producer",
    which the pre-existing `_call_kind` classifier (engine.py) already
    matches to `publishes` -- confirms the "best-effort, no MessageContract"
    framing holds without any change, not left untested."""
    (tmp_path / "publisher.py").write_text(
        "def publish(event):\n"
        "    producer.send('orders.created', value=event)\n"
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "python")

    assert any(edge.target == "producer.send" and edge.kind == "publishes" for edge in result.edges)
    assert result.message_contracts == []


def test_node_analyzer_skips_kafka_consumer_pairing_when_multiple_subscriptions_exist(tmp_path: Path):
    """Two `.subscribe({topic})` calls in the same file make pairing a
    `.run()` handler to a specific topic ambiguous -- skipped, not guessed."""
    (tmp_path / "consumer.ts").write_text(
        '''import { Kafka } from "kafkajs";
const kafka = new Kafka({ brokers: ["localhost:9092"] });
const consumer = kafka.consumer({ groupId: "orders" });
async function run() {
  await consumer.subscribe({ topic: "orders.created" });
  await consumer.subscribe({ topic: "payments.created" });
  await consumer.run({ eachMessage: async ({ message }) => { await orderService.handle(message); } });
}
''',
        encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.entrypoints == []


def test_node_analyzer_skips_literal_topic_when_consumer_also_subscribes_dynamically(tmp_path: Path):
    (tmp_path / "consumer.ts").write_text(
        '''import { Kafka } from "kafkajs";
const kafka = new Kafka({ brokers: ["localhost:9092"] });
const consumer = kafka.consumer({ groupId: "orders" });
async function run(topic) {
  await consumer.subscribe({ topic: "orders.created" });
  await consumer.subscribe({ topic });
  await consumer.run({ eachMessage: async ({ message }) => { await handle(message); } });
}
''', encoding="utf-8",
    )

    result = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert result.message_contracts == []
    assert not any(entry.kind == "message" for entry in result.entrypoints)
