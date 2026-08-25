import { useTranslation } from "react-i18next";
import { ChartBar, ListDots, TerminalWindow } from "@/lib/icons";

import { PageHeader } from "@/components/PageHeader";
import { EventLog } from "@/components/statistics/EventLog";
import { ServiceLog } from "@/components/statistics/ServiceLog";
import { TrafficOverview } from "@/components/statistics/TrafficOverview";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

/*
 * Statistics and logs, which is three questions rather than one.
 *
 * Traffic is what the tunnel carried. Activity is what was done to it, and by
 * whom. The service log is what the processes running it have been saying. They
 * share a page because they are all read backwards through time and because the
 * question that brings somebody here - "what happened to this?" - is usually
 * answered by two of them together: the event log says a client stopped working
 * on Tuesday and why, and the journal says what the tunnel printed while it did.
 *
 * Tabs rather than three stacked cards, because only one of them is ever being
 * read and the other two are expensive to keep on screen: traffic polls the live
 * blob every couple of seconds and the journal forks journalctl. The inactive
 * tab is unmounted, so each of those costs nothing at all while somebody is
 * looking at one of the others - which is why each tab's content is a component
 * of its own rather than a block in this file.
 *
 * Traffic opens first. It is the one somebody arrives at from the dashboard, and
 * the logs are what they come back for.
 */

export default function Statistics(): JSX.Element {
  const { t } = useTranslation();

  return (
    <>
      <PageHeader
        title={String(t("statistics.title"))}
        description={String(t("statistics.subtitle"))}
        icon={ListDots}
      />

      <Tabs defaultValue="traffic">
        <TabsList>
          <TabsTrigger value="traffic">
            <ChartBar aria-hidden="true" />
            {t("statistics.tabs.traffic")}
          </TabsTrigger>
          <TabsTrigger value="events">
            <ListDots aria-hidden="true" />
            {t("statistics.tabs.events")}
          </TabsTrigger>
          <TabsTrigger value="service">
            <TerminalWindow aria-hidden="true" />
            {t("statistics.tabs.service")}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="traffic">
          <TrafficOverview />
        </TabsContent>

        <TabsContent value="events">
          <EventLog />
        </TabsContent>

        <TabsContent value="service">
          <ServiceLog />
        </TabsContent>
      </Tabs>
    </>
  );
}
