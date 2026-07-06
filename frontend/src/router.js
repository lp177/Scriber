// Hash-based router with a simple auth guard on the stored session token.
import { createRouter, createWebHashHistory } from "vue-router";
import LoginView from "./views/LoginView.vue";
import DashboardView from "./views/DashboardView.vue";
import SettingsView from "./views/SettingsView.vue";
import ParticipantsView from "./views/ParticipantsView.vue";
import UserView from "./views/UserView.vue";
import MeetingView from "./views/MeetingView.vue";

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView },
    { path: "/", name: "dashboard", component: DashboardView },
    { path: "/participants", name: "participants", component: ParticipantsView },
    { path: "/users/:id", name: "user", component: UserView },
    { path: "/meetings/:id", name: "meeting", component: MeetingView },
    { path: "/settings", name: "settings", component: SettingsView },
  ],
});

router.beforeEach((to) => {
  const token = localStorage.getItem("scriber_token");
  if (!token && to.name !== "login") {
    return { name: "login" };
  }
  if (token && to.name === "login") {
    return { name: "dashboard" };
  }
  return true;
});

export default router;
