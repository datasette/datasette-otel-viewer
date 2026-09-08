import { mount } from "svelte";
import IndexPage from "./IndexPage.svelte";
import "../../app.css";

const app = mount(IndexPage, {
  target: document.getElementById("app-root")!,
});

export default app;
