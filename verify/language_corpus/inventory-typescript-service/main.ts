import express, { type Request, type Response } from "express";

type Item = { id: string; name: string };
const app = express();
app.use(express.json());

app.get("/items/:id", (request: Request, response: Response<Item>) => {
  response.json({ id: request.params.id, name: "sample" });
});

app.post("/items", (request: Request<{}, Item, { name: string }>, response: Response<Item>) => {
  response.status(201).json({ id: "created", name: request.body.name });
});

app.listen(3000);
