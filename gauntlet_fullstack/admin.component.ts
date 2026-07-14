import { Component } from "@angular/core";
import { DomSanitizer } from "@angular/platform-browser";

@Component({
  selector: "gauntlet-admin",
  template: `
    <section>
      <img [src]="avatarUrl">
      <div [innerHTML]="announcementHtml"></div>
    </section>
  `,
})
export class AdminComponent {
  announcementHtml = "";
  avatarUrl = "";

  constructor(private sanitizer: DomSanitizer) {}

  trustOperatorHtml(value: string) {
    return this.sanitizer.bypassSecurityTrustHtml(value);
  }
}
