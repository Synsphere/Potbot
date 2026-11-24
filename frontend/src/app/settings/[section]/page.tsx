import { notFound } from "next/navigation";
import { SETTINGS_SECTIONS } from "../../../lib/settings.ts";
import type { SettingsSection } from "../../../lib/settings.ts";
import SettingsWorkbench from "../SettingsWorkbench.tsx";
export default async function SettingsSectionPage({params}:{params:Promise<{section:string}>}){
  const {section}=await params;
  if(!SETTINGS_SECTIONS.includes(section as SettingsSection))notFound();
  return <SettingsWorkbench section={section as SettingsSection}/>;
}
