# Brainstorm


deployed somewhere via docker or whatever, publicly accessible,  for examle aws ecs 

a deployed website connected to the container with teh app, it is a must have for the judging procedures.

automated testy
agent --> proxy --> dashboard


widoczne:
- duży dashboard z tracowaniem wszystkiego (zarowno metryki jak i odnosniki do konkretnych konwersacji, szczzegolnie tyhc oflagowanych, metryka ryzyko * impact), ogólnie widok z całego systemu i wszystko co chcieli obserwowac wg rules i guidelines. Przycisk do wyczyszczenia przechowywanych danych
- strona do czytania i zmian w konfiguracji
- chat console, mozna tam wysylac polecenia dla agenta i inne promty, jest tez tam jest tracing tego co się dzieje dla prompta który właśnie wpisałem, czyli taki mini dashboard ale tylko dla jednej kowersacji
- widok z testów, dla jakich tooli wyjebało i ile ich jest łącznie (np testujemy testy której kategorii). Przycisk do runowania wszysktich testow, przycisk do runowania konretnych grup testow. 



## Dwie ścieżki dla testow: 

1. bez AI po prostu, leci duzo automatycznych wywoaln tooli i innych zmockokwanych usecasow naszego guardraila. 
2. agentowy track gdzie faktycznie AI wywołuje te toole, tam jes tpodpeity rzecczywiscie ai. 


proces biznesowy



zbieramy dane o pracy agenta - tracing, np historię promptów i flagujemy odpowiednio te które były problematyczne. każdy z tych jest oznaczony inaczej (color coded): ALLOW, BLOCK, REDACT, APPROVE, ALERT
