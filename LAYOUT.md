# Generic layout sketches

## Main Screen

```
+---------------------------------------------------------------------------+
| datamanager | <header>                                                    |
+-------------+-------------------------------------------------------------+
| [configure] | <content pane>                                              |
| [build]     |                                                             |
| [repo]      |                                                             |
| [deploy]    |                                                             |
|             |                                                             |
|             |                                                             |
| [settings]  |                                                             |
+-------------+-------------------------------------------------------------+
```

## Configure Screen

```
                Dropdown    Button new configration
                    V               V
+---------------------------------------------------------------------------+
| datamanager | [dev-mini v] [New Configuration]                            |
+-------------+-------------------------------------------------------------+
| [configure] | map | tab .. | tab .. |                                     |
| [build]     |-------------------------------------------------------------|
| [repo]      | <tab content>                                               |
| [deploy]    |                                                             |
|             |                                                             |
|             |                                                             |
| [settings]  |                                                             |
+-------------+-------------------------------------------------------------+
```

### Map tab content

``` 
Region list
Each region has a 
color box in front
    V
+---------------------------------------------------------------------------+
| Regions     |                                                             |
+-------------| <map view>                                                  |
| [] benelux  | Here you can assign / deassign countries to the selected    |
| [] france   | region                                                      |
| [] germany  |                                                             |
|             | Each region has an assigned color, the selected countries   |
|             | are shaded in the color of the region                       |
| [ Add ]     |                                                             |
+-------------+-------------------------------------------------------------+
    ^
Button to Add
a region

```
