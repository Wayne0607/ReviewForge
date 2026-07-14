import { AdminComponent } from "gauntlet_fullstack/admin.component";

export class AngularBridge {
  constructor(private admin: AdminComponent) {}

  showMessage(rawHtml: string) {
    return this.admin.trustOperatorHtml(rawHtml);
  }

  renderTemplate(rawHtml: string) {
    const holder = document.createElement("div");
    holder.innerHTML = rawHtml;
    return holder;
  }
}
