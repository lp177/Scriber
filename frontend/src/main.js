// Application entry point for the Scriber admin dashboard.
import { createApp } from "vue";
import App from "./App.vue";
import router from "./router.js";
import { initRipple } from "./ripple.js";
import "./style.css";

createApp(App).use(router).mount("#app");
initRipple();
