export default function App() {
  const load = (id) => fetch(`/api/orders/${id}`).then((r) => r.json());
  return <button onClick={() => load(1)}>Load order</button>;
}
