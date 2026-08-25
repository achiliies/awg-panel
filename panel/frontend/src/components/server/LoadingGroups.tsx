import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

export interface LoadingGroupsProps {
  /** How many cards the page settles into, so the placeholder is the right height. */
  cards?: number;
}

/** Cards' worth of placeholder: these pages are forms, not spinners. */
export function LoadingGroups({ cards = 3 }: LoadingGroupsProps): JSX.Element {
  return (
    <div className="space-y-4">
      {Array.from({ length: cards }, (_value, card) => (
        <Card key={card}>
          <CardHeader>
            <Skeleton className="h-5 w-40" />
            <Skeleton className="h-4 w-full max-w-md" />
          </CardHeader>
          <CardContent className="grid gap-6 sm:grid-cols-2">
            {[0, 1, 2, 3].map((field) => (
              <div key={field} className="space-y-2">
                <Skeleton className="h-4 w-32" />
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-3 w-48" />
              </div>
            ))}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
