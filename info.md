[![Stars][stars-shield]][chihiros_led_control]
[![hacs][hacsbadge]][hacs]

<!--[![BuyMeCoffee][buymecoffeebadge]][buymecoffee] -->

[![Community Forum][forum-shield]][forum]

{% if prerelease %}
### NB!: This is a Beta version!

{% endif %}

_Component to integrate with [Chihiros][chihiros_aquatic_studio] lights._

![Chihiros Aquatic Studio][chihiros_icon]

**This component will set up the following platforms.**

| Platform              |Description                          |
| --------------------- | ----------------------------------- |
| `light`               | BLE Lights                          |
| `switch`              | Toggle between manual and auto mode |


Tested devices:
* Tiny Terrarium Egg
* LED A2

{% if not installed %}
## Installation

1. Click install.
2. In the HA UI go to "Configuration" -> "Integrations"
3. Your Chihiros LED should be auto discovered, if not click "+" and search for "Chihiros".

{% endif %}


## Configuration is done in the UI

<!---->

<!--
<a href="https://www.buymeacoffee.com/tschamm" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>

-->
***

[chihiros_aquatic_studio]: https://www.chihirosaquaticstudio.com
[chihiros_led_control]:https://github.com/TheMicDiet/chihiros-led-control
[chihiros_icon]: https://www.chihirosaquaticstudio.com/cdn/shop/files/logo_cb46bbf4-c367-49c2-a99a-e920069e8d9c.png
[stars-shield]: https://img.shields.io/github/stars/TheMicDiet/chihiros-led-control
[hacs]: https://github.com/custom-components/hacs
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg
[forum]: https://community.home-assistant.io/
[license]: https://github.com/TheMicDiet/chihiros-led-control/blob/main/LICENSE
