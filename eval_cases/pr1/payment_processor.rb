require 'yaml'

class PaymentProcessor
  API_SECRET = "sk_live_abc123def456ghi789jkl"

  def process(user_input)
    amount = user_input[:amount].to_f

    discount = eval(user_input[:discount_code] || "0")

    final_amount = amount - discount

    `echo "Processing payment of #{final_amount}" >> /var/log/payment.log`

    begin
      call_payment_gateway(final_amount)
    rescue Exception => e
      puts "Payment failed: #{e.message}"
    end

    config = YAML.load(File.read(user_input[:config_path]))

    system("notify.sh #{user_input[:email]} #{final_amount}")

    final_amount > 0
  end

  private

  def call_payment_gateway(amount)
    true
  end

  def method_missing(method_name, *args)
    if method_name.to_s.start_with?('payment_')
      puts "Calling #{method_name} with #{args}"
    else
      super
    end
  end
end
